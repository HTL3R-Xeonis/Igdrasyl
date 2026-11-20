"""
The names an ESXi VM's network interfaces really have inside the guest.

The topology names an interface `eth0`, `eth1`, ... but a VM on ESXi never gets that name: Linux calls its vmxnet3 NICs
`ens160`, `ens192`, `ens224` (by PCI slot; a hot-added NIC gets yet another one that sorts second although it is the newest),
Windows `Ethernet0`, `Ethernet1`, ... (by device order). Commands written for `eth0` therefore address an interface that does
not exist. The one thing both sides know is the MAC address: the NIC that sits on the port group `<node>_<interface>` has a MAC
in the VM's configuration, and the guest lists the same MAC next to its own name. This module reads both and joins them.

Nothing is stored: every call asks ESXi and the guest again, and the result lives as long as the draft that needed it.
A failure never raises: the caller gets what could be resolved plus a warning, and falls back to the topology names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from src.settings import Settings

from app import config_push
from app.guest_ops import guest_credentials_for_image, run_command_in_guest

FALLBACK_WARNING = "could not resolve guest interface names, topology names used"

_MAC = r"[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}"
# `ip -o link`: "3: ens224: <BROADCAST,...> mtu 1500 ... \    link/ether 00:50:56:aa:bb:cc brd ff:ff:ff:ff:ff:ff"
# A VLAN subinterface prints "eth0.10@eth0"; it shares its parent's MAC and is not a NIC of its own.
_IP_LINK_LINE = re.compile(
    rf"^\s*\d+:\s+(?P<name>[^\s:@]+)(?P<peer>@\S+)?:.*?\blink/ether\s+(?P<mac>{_MAC})\b"
)
_MAC_IN_LINE = re.compile(_MAC)

_LINUX_COMMAND = "ip -o link"
# One "name|mac" per line is easy to read back; Get-NetAdapter's own table is parsed as well (see parse_windows).
_WINDOWS_COMMAND = (
    "Get-NetAdapter | ForEach-Object { '{0}|{1}' -f $_.Name, $_.MacAddress }"
)


@dataclass
class GuestNames:
    """Topology interface name -> name inside the guest, for every interface that could be resolved."""

    names: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def normalize_mac(mac: str) -> str:
    return mac.strip().lower().replace("-", ":")


def parse_linux(output: str) -> dict[str, list[str]]:
    """MAC -> the guest interface names carrying it, from `ip -o link` (subinterfaces and loopback skipped)."""
    found: dict[str, list[str]] = {}
    for line in output.splitlines():
        match = _IP_LINK_LINE.match(line)
        if match is None or match["peer"]:
            continue
        found.setdefault(normalize_mac(match["mac"]), []).append(match["name"])
    return found


def parse_windows(output: str) -> dict[str, list[str]]:
    """
    MAC -> adapter names, from `Name|MAC` lines or from the table Get-NetAdapter prints (Name, optionally more columns,
    then the MAC): the name is whatever stands before the MAC, so names with a space ("Ethernet 2") survive. Lines without a
    MAC (headers, rulers, messages) are skipped.
    """
    found: dict[str, list[str]] = {}
    for line in output.splitlines():
        match = _MAC_IN_LINE.search(line)
        if match is None:
            continue
        name = line[: match.start()].strip(" \t|")
        # table form "Ethernet0  vmxnet3 Ethernet Adapter  #2  12  Up  00-50-..." -> the first column is the name
        if "|" not in line[: match.start()]:
            name = re.split(r"\s{2,}", name)[0]
        if name:
            found.setdefault(normalize_mac(match.group(0)), []).append(name)
    return found


def port_group_of(node: Any, interface: str) -> str:
    """The ESXi port group of a topology interface: `<node>_<interface>` with `/` replaced by `-` (VirtualLan.name)."""
    vlan = getattr(getattr(node, "interfaces", {}).get(interface), "vlan", None)
    name = getattr(vlan, "name", None)
    if isinstance(name, str) and name:
        return name
    return f"{node.name}_{interface.replace('/', '-')}"


def _tools_mac(vm: Any, port_group: str) -> str | None:
    """
    The MAC that VMware Tools reports for the NIC on `port_group`, i.e. the one the RUNNING guest really has. It differs
    from the MAC in the VM's configuration while a vCenter has just switched the generated MACs (00:0c:29 -> 00:50:56)
    and the guest has not been rebooted since: the join over the configured MAC then finds nothing. The two are tied
    together by the virtual device key. None when anything of this cannot be read.
    """
    try:
        from pyVmomi import vim

        key = next(
            device.key
            for device in vm.config.hardware.device
            if isinstance(device, vim.vm.device.VirtualEthernetCard)
            and getattr(device.backing, "deviceName", None) == port_group
        )
        for nic in vm.guest.net or []:
            mac = getattr(nic, "macAddress", None)
            if nic.deviceConfigId == key and isinstance(mac, str) and mac.strip():
                return normalize_mac(mac)
    except (
        Exception
    ):  # only a second chance to find the NIC, never a failure of its own
        return None
    return None


def _guest_read(esxi: Any, vm: Any, image: str, windows: bool) -> str:
    """The guest's own NIC list (text). Raises RuntimeError with a reason that never contains a credential."""
    from pyVmomi import vim

    creds = guest_credentials_for_image(image)
    if creds is None:
        raise RuntimeError("no guest credentials for this image")
    auth = vim.vm.guest.NamePasswordAuthentication(username=creds[0], password=creds[1])
    guest_ops = esxi.content.guestOperationsManager
    command = (
        config_push.windows_powershell_command(_WINDOWS_COMMAND)
        if windows
        else _LINUX_COMMAND
    )
    exit_code, output = run_command_in_guest(
        guest_ops.processManager,
        vm,
        auth,
        command,
        windows,
        file_manager=guest_ops.fileManager,
        esxi_host=Settings.ESXI.IP,
    )
    if exit_code != 0:
        raise RuntimeError(f"the NIC list command failed (exit code {exit_code})")
    return output


def resolve(app: Any, node: Any, interfaces: list[str]) -> GuestNames:
    """
    The guest names of `interfaces` (topology names) of a Linux or Windows VM on ESXi. Interfaces that can not be
    resolved are left out and named in a warning; if nothing can be read at all the warning is FALLBACK_WARNING.
    """
    result = GuestNames()
    if not interfaces:
        return result
    try:
        names = _resolve(app, node, sorted(set(interfaces)), result)
    except Exception as exc:
        # Never log text of the exception: a guest-ops fault may quote the login it was given.
        logger.warning(
            "Guest interface names of '{}' not resolved ({})",
            getattr(node, "name", "?"),
            type(exc).__name__,
        )
        result.names.clear()
        result.warnings = [FALLBACK_WARNING]
        return result
    result.names.update(names)
    return result


def _resolve(
    app: Any, node: Any, interfaces: list[str], result: GuestNames
) -> dict[str, str]:
    esxi = app.state.esxi_connection
    vm = esxi.get_vm(node.name)
    if vm is None:
        raise RuntimeError("the VM was not found on ESXi")
    macs: dict[str, str] = {}
    for interface in interfaces:
        mac = esxi.find_vm_nic_mac_by_port_group(vm, port_group_of(node, interface))
        if isinstance(mac, str) and mac.strip():
            macs[interface] = normalize_mac(mac)
    if not macs:
        raise RuntimeError(
            "no NIC of the VM is attached to the interfaces' port groups"
        )
    if not config_push._guest_operations_ready(vm):
        raise RuntimeError("VMware Tools is not running")
    windows = "windows" in str(node.image).lower()
    output = _guest_read(esxi, vm, node.image, windows)
    guest = (parse_windows if windows else parse_linux)(output)
    if not guest:
        raise RuntimeError("the guest's NIC list could not be read")

    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    for interface in interfaces:
        candidates = guest.get(macs.get(interface, ""), [])
        if not candidates and interface in macs:
            # the configured MAC is not in the guest (vCenter switched the MACs, the guest keeps the old ones until its
            # next reboot): ask VMware Tools which MAC the guest has on that NIC
            tools_mac = _tools_mac(vm, port_group_of(node, interface))
            if tools_mac is not None:
                candidates = guest.get(tools_mac, [])
        if len(candidates) == 1:
            resolved[interface] = candidates[0]
        else:
            unresolved.append(interface)
    if not resolved:
        raise RuntimeError("no MAC of the VM's NICs was found in the guest")
    if len(unresolved) == 1:
        result.warnings.append(
            f"could not resolve the guest interface name of {unresolved[0]}, topology name used"
        )
    elif unresolved:
        result.warnings.append(
            f"could not resolve the guest interface names of {', '.join(unresolved)}, topology names used"
        )
    return resolved
