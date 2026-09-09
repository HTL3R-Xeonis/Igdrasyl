"""
Config-push engine: sends a device-name-keyed list of raw CLI commands to
each device, transport auto-selected from its role/env (already known
from the live topology graph) - GNS3 console devices go over Telnet via
Netmiko, ESXi-hosted VMs go over pyvmomi's Guest Operations API. Per the
project's design (see HANDOFF.md): stop pushing a device's remaining
commands on its first failure, but always continue on to the next
device - implemented by catching/recording failures per-device rather
than letting one raise out of push_all.

Deliberately NOT using Netmiko's send_config_set()/enable() abstractions
- commands are sent exactly as given, one at a time, uniformly across
the whole heterogeneous device mix (Cisco IOS, VPCS, Linux, Windows).
That's the whole reason this project's config file holds raw command
lists instead of a structured/templated schema: a uniform "send this
verbatim" transport works across all of them without a per-vendor
renderer. send_command_timing() (not the stricter, prompt-pattern-based
send_command()) is used for the same reason - it tolerates the prompt
actually changing mid-sequence (e.g. after "configure terminal"),
which a family of commands representing modal CLI transitions needs.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel

from app.gns3_state import NoLiveProject, list_live_nodes
from src.connections.api_handler import APIHandler
from src.graph.blocks.generic_node import GenericNode
from src.graph.environment import Environment, normalize_template_name
from src.settings import Settings

_NETMIKO_CONNECT_TIMEOUT_SECONDS = 20
_GUEST_OPS_TIMEOUT_SECONDS = 60
_GUEST_OPS_POLL_INTERVAL_SECONDS = 1.0

# Minimal, deliberately narrow: only recognized for the cisco_ios_telnet
# device type, where these prefixes are well-established. No attempt is
# made to detect failures on the generic_telnet fallback (VPCS, unknown
# devices) - there's no reliable, vendor-neutral signal for that, so
# those commands are always recorded as "ok" (best-effort, not verified).
_IOS_ERROR_MARKERS = ("% invalid", "% incomplete", "% unrecognized", "% ambiguous")


class CommandResult(BaseModel):
    command: str
    output: str
    ok: bool


class DeviceResult(BaseModel):
    device: str
    transport: str
    ok: bool
    commands: list[CommandResult] = []
    error: str | None = None


def _netmiko_device_type(image: str) -> str:
    if "cisco ios" in image.lower():
        return "cisco_ios_telnet"
    return "generic_telnet"


def _looks_like_ios_error(output: str) -> bool:
    lowered = output.lower()
    return any(marker in lowered for marker in _IOS_ERROR_MARKERS)


def _push_via_netmiko(
    node: GenericNode, commands: list[str], gns3_vm_ip: str, console_port: int
) -> DeviceResult:
    from netmiko import ConnectHandler
    from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException

    device_type = _netmiko_device_type(node.image)
    try:
        conn = ConnectHandler(
            device_type=device_type,
            host=gns3_vm_ip,
            port=console_port,
            username="",
            password="",
            timeout=_NETMIKO_CONNECT_TIMEOUT_SECONDS,
        )
    except (NetmikoTimeoutException, NetmikoAuthenticationException, OSError) as exc:
        return DeviceResult(
            device=node.name, transport="netmiko", ok=False,
            error=f"couldn't connect: {exc}",
        )

    results: list[CommandResult] = []
    overall_ok = True
    try:
        for command in commands:
            output = conn.send_command_timing(command)
            failed = device_type == "cisco_ios_telnet" and _looks_like_ios_error(output)
            results.append(CommandResult(command=command, output=output, ok=not failed))
            if failed:
                overall_ok = False
                break
    finally:
        conn.disconnect()

    return DeviceResult(
        device=node.name, transport="netmiko", ok=overall_ok, commands=results
    )


def _guest_credentials_for_image(image: str) -> tuple[str, str] | None:
    """
    First user/password pair (parallel lists, index-matched) from the
    ESXi Template-API's per-template metadata for the given image - see
    HANDOFF.md, confirmed live on this project's real infrastructure
    (e.g. debian: users=[junioradmin, root], passwords=[junioradmin,
    REMOVED]). Mirrors APIHandler.find_esxi_template_file's own
    lenient-name-match pattern rather than adding a new endpoint.
    """
    data = APIHandler.get(f"{Settings.API.ESXI_TEMPLATE_SERVER_URL}/api/templates")
    normalized = normalize_template_name(image)
    for template in data["templates"]:
        if normalize_template_name(template["name"]) != normalized:
            continue
        users = template.get("users") or []
        passwords = template.get("passwords") or []
        if users and passwords:
            return users[0], passwords[0]
        return None
    return None


def _wait_for_guest_process(process_manager, vm, auth, pid: int) -> int | None:
    """Polls until the guest process exits, or returns None on timeout."""
    deadline = time.monotonic() + _GUEST_OPS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        procs = process_manager.ListProcessesInGuest(vm, auth, [pid])
        if procs and procs[0].endTime is not None:
            return procs[0].exitCode
        time.sleep(_GUEST_OPS_POLL_INTERVAL_SECONDS)
    return None


def _push_via_guest_ops(
    node: GenericNode, commands: list[str], esxi_connection: Any
) -> DeviceResult:
    from pyVmomi import vim

    vm = esxi_connection.get_vm(node.name)
    if vm is None:
        return DeviceResult(
            device=node.name, transport="guest_ops", ok=False,
            error=f"VM '{node.name}' not found on ESXi",
        )

    creds = _guest_credentials_for_image(node.image)
    if creds is None:
        return DeviceResult(
            device=node.name, transport="guest_ops", ok=False,
            error=f"no guest credentials found for image '{node.image}'",
        )
    username, password = creds
    auth = vim.vm.guest.NamePasswordAuthentication(username=username, password=password)
    process_manager = esxi_connection.content.guestOperationsManager.processManager
    is_windows = "windows" in node.image.lower()

    results: list[CommandResult] = []
    overall_ok = True
    for command in commands:
        if is_windows:
            spec = vim.vm.guest.ProcessManager.ProgramSpec(
                programPath="C:\\Windows\\System32\\cmd.exe",
                arguments=f"/c {command}",
            )
        else:
            spec = vim.vm.guest.ProcessManager.ProgramSpec(
                programPath="/bin/bash", arguments=f'-c "{command}"'
            )
        try:
            pid = process_manager.StartProgramInGuest(vm, auth, spec)
        except vim.fault.MethodFault as exc:
            results.append(CommandResult(command=command, output=str(exc), ok=False))
            overall_ok = False
            break

        exit_code = _wait_for_guest_process(process_manager, vm, auth, pid)
        ok = exit_code == 0
        output = "timed out waiting for exit" if exit_code is None else f"exit code {exit_code}"
        results.append(CommandResult(command=command, output=output, ok=ok))
        if not ok:
            overall_ok = False
            break

    return DeviceResult(
        device=node.name, transport="guest_ops", ok=overall_ok, commands=results
    )


def push_all(devices: dict[str, list[str]], app: Any) -> list[DeviceResult]:
    results: list[DeviceResult] = []

    live_by_name: dict[str, dict] = {}
    live_error: str | None = None
    try:
        live_by_name = {n["name"]: n for n in list_live_nodes(app)}
    except NoLiveProject as exc:
        live_error = str(exc)

    for device_name, commands in devices.items():
        node = app.state.graph.nodes.get(device_name)
        if node is None:
            results.append(
                DeviceResult(
                    device=device_name, transport="n/a", ok=False,
                    error="not found in topology",
                )
            )
            continue

        if node.env == Environment.ON_GNS3:
            live = live_by_name.get(device_name)
            if live is None:
                results.append(
                    DeviceResult(
                        device=device_name, transport="netmiko", ok=False,
                        error=live_error or "no live console info for this node",
                    )
                )
                continue
            console_port = live.get("console")
            if live.get("console_type") != "telnet" or console_port is None:
                results.append(
                    DeviceResult(
                        device=device_name, transport="netmiko", ok=False,
                        error=f"no telnet console available (type={live.get('console_type')!r})",
                    )
                )
                continue
            results.append(
                _push_via_netmiko(node, commands, app.state.gns3_vm_ip, console_port)
            )
        elif node.env == Environment.ON_ESXI:
            results.append(_push_via_guest_ops(node, commands, app.state.esxi_connection))
        else:
            results.append(
                DeviceResult(
                    device=device_name, transport="n/a", ok=False,
                    error=f"node environment {node.env.name} has no config-push transport",
                )
            )

    return results
