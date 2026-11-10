"""
Connectivity test: do the lab's VMs actually reach each other over the lab network?

/maintenance/verify only checks the wiring (ports, port groups, links). This puts real traffic
on it: the VMs that share one layer-2 segment (a switch, or a direct link) get a temporary
address each, every one pings every other, and the addresses are taken away again - also when
something failed in between. The pushes go through the same config-push engine as everything
else, so they work on any VM with VMware Tools.

VMs behind a router are not in one segment and are not tested against each other: without
routes configured on the router they would fail for a reason that has nothing to do with the
wiring. A VM with no peer in its segment is reported as skipped.

The addresses come from 198.18.0.0/15 (the benchmarking range of RFC 2544), one /24 per segment,
so they never collide with a lab's own addressing or with the internet.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from loguru import logger
from src.graph.environment import Environment

from app.config_push import DeviceResult, push_all

HOST_ROLES = ("VM", "PC")
SWITCH_ROLES = ("SWITCH",)
FIRST_SEGMENT_OCTET = 200
MAX_COUNT = 100
MAX_SIZE = 1400
FIREWALL_RULE_NAME = "xeonis-connectivity-test"
PREFIX_LENGTH = 24

# Linux: the first interface that is not the loopback. NetworkManager is told to leave it alone
# for the test (a manually added address would be removed again otherwise, as seen on Rocky Linux).
_LINUX_FIND_INTERFACE = (
    "IF=$(ls /sys/class/net | grep -v '^lo$' | head -n 1); "
    '[ -n "$IF" ] || { echo "no network interface"; exit 1; }; '
)
# Windows: the first adapter that is up.
_WINDOWS_FIND_ADAPTER = (
    "$a = Get-NetAdapter | Where-Object Status -eq 'Up' | Select-Object -First 1; "
    "if (-not $a) { Write-Output 'no network adapter is up'; exit 1 }; "
)


class AlreadyRunning(Exception):
    """A connectivity test or a config push is already in progress."""


class NothingToTest(Exception):
    """No two VMs share a switch or a link, or the named ones do not."""


@dataclass
class Host:
    name: str
    address: str
    windows: bool
    segment: int


@dataclass
class ConnectivityJob:
    id: str
    status: str = "running"  # "running" | "done" | "error"
    phase: str = "planning"
    started: float = field(default_factory=time.monotonic)
    segments: list[dict[str, Any]] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    pairs: list[dict[str, Any]] = field(default_factory=list)
    problems: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "phase": self.phase if self.status == "running" else "",
            "elapsed_seconds": round(time.monotonic() - self.started, 1),
            "segments": self.segments,
            "skipped": self.skipped,
            "pairs": self.pairs,
            "problems": self.problems,
            "all_ok": self.status == "done"
            and not self.problems
            and bool(self.pairs)
            and all(pair["received"] == pair["sent"] for pair in self.pairs),
            "error": self.error,
        }


# ---------------------------------------------------------------- planning


def _segments(
    graph: Any, wanted: list[str] | None
) -> tuple[list[list[str]], dict[str, str]]:
    """
    The groups of VM names that share one layer-2 segment (only switches and direct links in
    between, no router), and why every other VM is left out.
    """
    nodes = graph.nodes
    parent: dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for node in nodes.values():
        if node.role not in HOST_ROLES + SWITCH_ROLES:
            continue
        find(node.name)
        for interface_name in node.interfaces:
            neighbour = node.get_neighbour(interface_name)
            if neighbour is not None and neighbour.role in HOST_ROLES + SWITCH_ROLES:
                parent[find(node.name)] = find(neighbour.name)

    groups: dict[str, list[str]] = {}
    skipped: dict[str, str] = {}
    for node in nodes.values():
        if node.role not in HOST_ROLES:
            continue
        if node.env != Environment.ON_ESXI:
            skipped[node.name] = "not an ESXi VM (no way to run commands in it)"
        elif wanted is not None and node.name not in wanted:
            continue
        else:
            groups.setdefault(find(node.name), []).append(node.name)

    segments = []
    for members in groups.values():
        if len(members) < 2:
            skipped[members[0]] = (
                "no other VM in the same switch segment (behind a router)"
            )
        else:
            segments.append(sorted(members))
    return sorted(segments), skipped


def plan(
    graph: Any, wanted: list[str] | None = None
) -> tuple[list[Host], dict[str, str]]:
    """
    :raises NothingToTest: nothing to ping.
    :raises ValueError: a wanted name is not a VM of the topology.
    """
    if wanted is not None:
        unknown = [name for name in wanted if name not in graph.nodes]
        if unknown:
            raise ValueError(f"not in the topology: {', '.join(unknown)}")
        not_vms = [name for name in wanted if graph.nodes[name].role not in HOST_ROLES]
        if not_vms:
            raise ValueError(
                f"not a VM or PC (only those can be tested): {', '.join(not_vms)}"
            )
    segments, skipped = _segments(graph, wanted)
    if not segments:
        raise NothingToTest(
            "no two VMs share a switch or a direct link - nothing to ping "
            "(VMs behind a router are not tested against each other)"
        )
    if len(segments) > 255 - FIRST_SEGMENT_OCTET:
        raise NothingToTest(
            f"too many separate segments ({len(segments)}): test a subset with 'nodes'"
        )
    hosts: list[Host] = []
    for index, members in enumerate(segments):
        for number, name in enumerate(members, 1):
            hosts.append(
                Host(
                    name=name,
                    address=f"198.18.{FIRST_SEGMENT_OCTET + index}.{number}",
                    windows="windows" in graph.nodes[name].image.lower(),
                    segment=index,
                )
            )
    return hosts, skipped


# ---------------------------------------------------------------- commands


STATE_FILE_LINUX = "/tmp/xeonis-connectivity.state"
STATE_FILE_WINDOWS = "C:\\Windows\\Temp\\xeonis-connectivity.state"


def setup_commands(host: Host) -> list[str]:
    """
    Gives the VM its test address. Whatever the setup changes besides the address (a Windows
    adapter's DHCP mode, a Linux interface that was down) is written to a state file first, so
    the clean-up puts back exactly that and nothing more - a VM that was configured statically
    must stay configured statically.
    """
    if host.windows:
        return [
            (
                f'powershell -NoProfile -Command "{_WINDOWS_FIND_ADAPTER}'
                f"$d = (Get-NetIPInterface -InterfaceIndex $a.ifIndex -AddressFamily IPv4).Dhcp; "
                f"Set-Content -Path {STATE_FILE_WINDOWS} -Value ('dhcp=' + $d); "
                f"if ($d -eq 'Enabled') {{ Set-NetIPInterface -InterfaceIndex $a.ifIndex -Dhcp Disabled }}; "
                f"New-NetIPAddress -InterfaceIndex $a.ifIndex -IPAddress {host.address} "
                f"-PrefixLength {PREFIX_LENGTH} | Out-Null; "
                f"New-NetFirewallRule -DisplayName {FIREWALL_RULE_NAME} -Direction Inbound "
                f"-Protocol ICMPv4 -IcmpType 8 -Action Allow | Out-Null; "
                f'Write-Output $a.Name"'
            )
        ]
    return [
        _LINUX_FIND_INTERFACE
        + 'if [ $(( $(cat "/sys/class/net/$IF/flags") & 1 )) -eq 0 ]; '
        + f"then echo link-was-down > {STATE_FILE_LINUX}; fi; "
        + 'ip link set "$IF" up; '
        + 'if command -v nmcli >/dev/null 2>&1; then nmcli device set "$IF" managed no || true; fi; '
        + f'ip addr add {host.address}/{PREFIX_LENGTH} dev "$IF" && echo "$IF {host.address}"'
    ]


def cleanup_commands(host: Host) -> list[str]:
    """Undoes setup_commands - safe to run when the setup failed half way or never ran."""
    if host.windows:
        return [
            (
                f'powershell -NoProfile -Command "{_WINDOWS_FIND_ADAPTER}'
                f"Remove-NetIPAddress -InterfaceIndex $a.ifIndex -IPAddress {host.address} "
                f"-Confirm:$false -ErrorAction SilentlyContinue; "
                f"if ((Test-Path {STATE_FILE_WINDOWS}) -and "
                f"((Get-Content {STATE_FILE_WINDOWS}) -eq 'dhcp=Enabled')) "
                f"{{ Set-NetIPInterface -InterfaceIndex $a.ifIndex -Dhcp Enabled }}; "
                f"Remove-Item {STATE_FILE_WINDOWS} -ErrorAction SilentlyContinue; "
                f"Remove-NetFirewallRule -DisplayName {FIREWALL_RULE_NAME} -ErrorAction SilentlyContinue; "
                f'Write-Output done"'
            )
        ]
    return [
        _LINUX_FIND_INTERFACE
        + f'ip addr del {host.address}/{PREFIX_LENGTH} dev "$IF" 2>/dev/null; '
        + 'if command -v nmcli >/dev/null 2>&1; then nmcli device set "$IF" managed yes || true; fi; '
        + f'if [ -f {STATE_FILE_LINUX} ]; then ip link set "$IF" down; rm -f {STATE_FILE_LINUX}; fi; '
        + "echo done"
    ]


def ping_command(source: Host, target: Host, count: int, size: int) -> str:
    if source.windows:
        return f"ping -n {count} -w 2000 -l {size} {target.address}"
    return f"ping -c {count} -W 2 -s {size} -q {target.address}"


_LINUX_COUNTS = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")
_LINUX_RTT = re.compile(r"= [\d.]+/([\d.]+)/")
_WINDOWS_COUNTS = re.compile(
    r"(?:Sent|Gesendet)\s*=\s*(\d+),\s*(?:Received|Empfangen)\s*=\s*(\d+)"
)
_WINDOWS_RTT = re.compile(r"(?:Average|Mittelwert)\s*=\s*(\d+)\s*ms")


def parse_ping(output: str, count: int) -> tuple[int, int, float | None]:
    """(sent, received, average round trip in ms or None) from a Linux or Windows ping."""
    counts = _LINUX_COUNTS.search(output) or _WINDOWS_COUNTS.search(output)
    if counts is None:
        return count, 0, None
    rtt = _LINUX_RTT.search(output) or _WINDOWS_RTT.search(output)
    return (
        int(counts.group(1)),
        int(counts.group(2)),
        float(rtt.group(1)) if rtt else None,
    )


# ---------------------------------------------------------------- run


def _failed(results: list[DeviceResult]) -> dict[str, str]:
    """device -> short reason, for every device whose push did not succeed."""
    problems: dict[str, str] = {}
    for result in results:
        if result.ok:
            continue
        last = result.commands[-1].output.strip() if result.commands else ""
        problems[result.device] = (result.error or last or "failed")[:300]
    return problems


def run(
    app: FastAPI,
    job: ConnectivityJob,
    hosts: list[Host],
    count: int,
    size: int,
    push=push_all,
) -> None:
    by_name = {host.name: host for host in hosts}
    attempted: list[Host] = []
    try:
        job.phase = "setting temporary addresses"
        # Before the push: even a setup that dies half way may have left an address behind.
        attempted = list(hosts)
        setup = push(
            {host.name: setup_commands(host) for host in hosts}, app, on_progress=None
        )
        job.problems.update({f"{n} (setup)": r for n, r in _failed(setup).items()})
        ready = {r.device for r in setup if r.ok}

        job.phase = "pinging"
        commands: dict[str, Any] = {}
        targets: dict[str, list[Host]] = {}
        for host in hosts:
            if host.name not in ready:
                continue
            peers = [
                other
                for other in hosts
                if other.segment == host.segment
                and other.name != host.name
                and other.name in ready
            ]
            if peers:
                targets[host.name] = peers
                commands[host.name] = {
                    "commands": [
                        ping_command(host, peer, count, size) for peer in peers
                    ],
                    "continue_on_error": True,
                }
        if commands:
            for result in push(commands, app, on_progress=None):
                peers = targets.get(result.device, [])
                if not result.commands and result.error:
                    job.problems[f"{result.device} (ping)"] = result.error[:300]
                for peer, command in zip(peers, result.commands, strict=False):
                    sent, received, rtt = parse_ping(command.output, count)
                    job.pairs.append(
                        {
                            "source": result.device,
                            "target": peer.name,
                            "target_address": peer.address,
                            "sent": sent,
                            "received": received,
                            "loss_percent": round(100 * (sent - received) / sent, 1)
                            if sent
                            else 100.0,
                            "rtt_avg_ms": rtt,
                        }
                    )
    except Exception as exc:
        job.error = str(exc) or type(exc).__name__
        logger.opt(exception=exc).error(f"Connectivity test {job.id} failed: {exc}")
    finally:
        job.phase = "removing the temporary addresses"
        try:
            if attempted:
                cleanup = push(
                    {
                        host.name: {
                            "commands": cleanup_commands(host),
                            "continue_on_error": True,
                        }
                        for host in attempted
                    },
                    app,
                    on_progress=None,
                )
                for name, reason in _failed(cleanup).items():
                    job.problems[f"{name} (cleanup)"] = (
                        f"{reason} - remove {by_name[name].address} by hand"
                    )
        except Exception as exc:
            job.problems["cleanup"] = (
                f"{exc} - the temporary 198.18.x.x addresses may remain"
            )
            logger.opt(exception=exc).error(f"Connectivity test cleanup failed: {exc}")
        job.status = "error" if job.error else "done"
        job.phase = ""


def start_job(
    app: FastAPI,
    wanted: list[str] | None = None,
    count: int = 10,
    size: int = 56,
) -> ConnectivityJob:
    """
    :raises AlreadyRunning: another connectivity test or a config push is running
    :raises NothingToTest: see plan()
    :raises ValueError: bad parameters or an unknown VM name
    """
    if not 1 <= count <= MAX_COUNT:
        raise ValueError(f"count must be between 1 and {MAX_COUNT}")
    if not 8 <= size <= MAX_SIZE:
        raise ValueError(f"size must be between 8 and {MAX_SIZE}")
    for other in (
        getattr(app.state, "connectivity_job", None),
        getattr(app.state, "push_job", None),
    ):
        if other is not None and other.status == "running":
            raise AlreadyRunning(f"job {other.id} is still running")
    hosts, skipped = plan(app.state.graph, wanted)
    job = ConnectivityJob(id=uuid.uuid4().hex, skipped=skipped)
    job.segments = [
        {
            "segment": index,
            "hosts": {h.name: h.address for h in hosts if h.segment == index},
        }
        for index in sorted({h.segment for h in hosts})
    ]
    app.state.connectivity_job = job
    threading.Thread(
        target=run, args=(app, job, hosts, count, size), daemon=True
    ).start()
    return job


def get_job(app: FastAPI, job_id: str) -> ConnectivityJob | None:
    job: ConnectivityJob | None = getattr(app.state, "connectivity_job", None)
    return job if job is not None and job.id == job_id else None
