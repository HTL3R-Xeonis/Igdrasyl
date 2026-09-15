"""
Embedded VM consoles via a network-backed virtual serial port
(vim.vm.device.VirtualSerialPort + URIBackingInfo) - both the WebMKS
ticket path and classic VNC-via-VMX are confirmed dead ends on this
project's real ESXi host (see HANDOFF.md: a deterministic bug in vmx's
own `mks` subsystem, reached the same way from both, unaffected by
debug/trivia logging, a VM power-cycle, or a full "Restart Management
Agents"). A network-backed serial port is a completely separate vmx
subsystem - plain TCP, no ticket/tunnel/rhttpproxy/mks involved at all -
confirmed working end-to-end live against a real VM this session: real
boot output, a genuine login prompt, confirmed two-way interactivity.

This is also why the frontend no longer needs noVNC/RFB: a serial
console is a plain-text TCP stream, the same shape as GNS3's own Telnet
consoles, so it reuses app/console.py's exact xterm.js/WebSocket
pattern (via app/tcp_bridge.py) instead of a binary-protocol client.

Requires a one-time-per-host firewall ruleset (`remoteSerialPort`,
confirmed via `esxcli network firewall ruleset list` against this
project's real host - inbound TCP 23 or 1024-65535, wide enough that no
new per-VM rule is needed) and, like VNC before it, a full power-off/
power-on cycle to add the new hardware - unlike VNC's own extraConfig
quirk, this one fails loudly (`InvalidPowerState`) on a running VM
rather than silently no-op'ing, so there's no "looked configured but
never started" trap to guard against here.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from typing import Any

from fastapi import APIRouter, WebSocket
from loguru import logger

from app.guest_ops import guest_credentials_for_image, run_command_in_guest
from app.tcp_bridge import bridge

router = APIRouter()

# A fixed, small pool (not a dynamic per-VM firewall rule) - same
# reasoning as VNC's own port range: reusing a handful of pre-opened
# ports is a smaller ask than a firewall change per VM.
_SERIAL_PORT_RANGE = range(2000, 2011)
_SERIAL_URI_PATTERN = re.compile(r"^tcp://(?::(\d+))?$")
_TASK_POLL_INTERVAL_SECONDS = 0.5
_SERIAL_READY_TIMEOUT_SECONDS = 60
_FIREWALL_RULESET = "remoteSerialPort"

# A small Python script (not a shell one-liner) specifically to avoid
# the nested-quoting problems a sed-based approach hit when run through
# guest-ops' single command-line interface this session - base64-encoded
# below and piped through `base64 -d` to sidestep quoting entirely.
_GRUB_FIX_SCRIPT = """
import re
path = "/etc/default/grub"
with open(path) as f:
    content = f.read()
if "console=ttyS0" not in content:
    content = re.sub(
        r'^GRUB_CMDLINE_LINUX_DEFAULT="([^"]*)"',
        r'GRUB_CMDLINE_LINUX_DEFAULT="\\1 console=ttyS0,115200n8"',
        content,
        flags=re.MULTILINE,
    )
    with open(path, "w") as f:
        f.write(content)
import subprocess
subprocess.run(["update-grub"], check=True)
print("done")
"""


def _wait_until_port_open(host: str, port: int, timeout_seconds: float) -> bool:
    """Polls a real TCP connect - True once something accepts it."""
    import socket

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=3):
                return True
        except OSError:
            time.sleep(2)
    return False


def _wait_for_task(task: Any) -> None:
    from pyVmomi import vim

    while task.info.state not in (vim.TaskInfo.State.success, vim.TaskInfo.State.error):
        time.sleep(_TASK_POLL_INTERVAL_SECONDS)
    if task.info.state == vim.TaskInfo.State.error:
        raise RuntimeError(str(task.info.error))


def _existing_serial_console(vm: Any) -> int | None:
    from pyVmomi import vim

    for device in vm.config.hardware.device:
        if not isinstance(device, vim.vm.device.VirtualSerialPort):
            continue
        backing = device.backing
        if not isinstance(backing, vim.vm.device.VirtualSerialPort.URIBackingInfo):
            continue
        match = _SERIAL_URI_PATTERN.match(backing.serviceURI or "")
        if match and match.group(1):
            return int(match.group(1))
    return None


def ensure_serial_console(vm: Any, assigned_ports: dict[str, int]) -> tuple[int, bool]:
    """
    Returns (port, was_just_enabled). was_just_enabled is True only the
    first time this VM gets a serial console added - the caller uses it
    to decide whether a disruptive power cycle is about to happen.
    :raises RuntimeError: if every port in _SERIAL_PORT_RANGE is already
        assigned to a different VM in this process's lifetime.
    """
    from pyVmomi import vim

    existing = _existing_serial_console(vm)
    if existing is not None:
        return existing, False

    used = set(assigned_ports.values())
    try:
        port = next(p for p in _SERIAL_PORT_RANGE if p not in used)
    except StopIteration:
        raise RuntimeError(
            f"no free serial port left in {_SERIAL_PORT_RANGE.start}-{_SERIAL_PORT_RANGE.stop - 1}"
        )

    from src.settings import Settings

    # vm.runtime.host is the vim.HostSystem this VM currently lives on -
    # no separate ESXiConnection reference needed to reach it.
    vm.runtime.host.configManager.firewallSystem.EnableRuleset(_FIREWALL_RULESET)

    backing = vim.vm.device.VirtualSerialPort.URIBackingInfo()
    backing.serviceURI = f"tcp://:{port}"
    backing.direction = "server"

    serial_port = vim.vm.device.VirtualSerialPort()
    serial_port.backing = backing
    serial_port.yieldOnPoll = True

    device_spec = vim.vm.device.VirtualDeviceSpec()
    device_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
    device_spec.device = serial_port

    spec = vim.vm.ConfigSpec()
    spec.deviceChange = [device_spec]

    # Confirmed empirically against this project's real infrastructure:
    # adding a VirtualSerialPort fails outright (InvalidPowerState) on a
    # running VM - unlike VNC's own extraConfig quirk, there's no
    # ambiguity here, so always power off first if needed.
    was_on = vm.runtime.powerState == vim.VirtualMachine.PowerState.poweredOn
    if was_on:
        _wait_for_task(vm.PowerOffVM_Task())

    _wait_for_task(vm.ReconfigVM_Task(spec))

    if was_on:
        _wait_for_task(vm.PowerOnVM_Task())

    if not _wait_until_port_open(Settings.ESXI.IP, port, _SERIAL_READY_TIMEOUT_SECONDS):
        raise RuntimeError(
            f"serial console on {vm.name} didn't come up within "
            f"{_SERIAL_READY_TIMEOUT_SECONDS}s of being added"
        )

    assigned_ports[vm.name] = port
    return port, True


def ensure_guest_console_configured(vm: Any, image: str, esxi_connection: Any) -> bool | None:
    """
    Best-effort: adds `console=ttyS0` to the guest's own GRUB config (via
    guest-ops), then power-cycles the VM so the serial console
    ensure_serial_console's own hardware/network path already provides
    actually shows something. Never raises - returns None if not
    attempted (a Windows image, per the same "windows" in image.lower()
    heuristic app/config_push.py already uses since Windows has no GRUB;
    VMware Tools isn't installed/running in the guest, so guest-ops
    can't run at all - confirmed live against a real OPNsense VM, a
    FreeBSD-based appliance that doesn't ship Tools by default and also
    has no `/etc/default/grub` to begin with, so this wouldn't have
    worked even with Tools; or no guest credentials found for this
    image), False if already configured (idempotent, no change made),
    True if it just configured it (and power-cycled the VM to pick it
    up).

    Confirmed live against real infrastructure: a soft guest-level
    reboot (`vm.RebootGuest()`) is NOT enough here - the guest itself
    comes back up correctly (kernel cmdline, `serial-getty@ttyS0`, all
    genuinely correct), but the vmx-level network-backed serial port
    listener doesn't reliably re-attach to the virtual UART afterward,
    so nothing reaches the network side despite the guest working
    perfectly. Only a real VM power cycle (a fresh vmx process) fixes
    this - the exact same pattern already true for
    ensure_serial_console's own hardware-add step and the classic-VNC
    bypass's `extraConfig` quirk before it. This makes this function
    genuinely disruptive on an already-configured VM's *first* setup
    (not just the initial hardware add), which is why it's still gated
    by the same idempotency check below - it only ever runs once per VM.
    """
    if "windows" in image.lower():
        return None
    if vm.guest.toolsRunningStatus != "guestToolsRunning":
        logger.warning(
            f"guest console=ttyS0 setup skipped for {vm.name}: VMware Tools "
            f"isn't running in the guest (toolsRunningStatus="
            f"{vm.guest.toolsRunningStatus!r}) - guest-ops can't run at all "
            f"without it, regardless of credentials"
        )
        return None

    try:
        creds = guest_credentials_for_image(image)
        if creds is None:
            return None
        username, password = creds

        from pyVmomi import vim

        auth = vim.vm.guest.NamePasswordAuthentication(username=username, password=password)
        process_manager = esxi_connection.content.guestOperationsManager.processManager

        exit_code, _output = run_command_in_guest(
            process_manager, vm, auth,
            "grep -q console=ttyS0 /etc/default/grub", is_windows=False,
        )
        if exit_code == 0:
            return False

        script_b64 = base64.b64encode(_GRUB_FIX_SCRIPT.encode()).decode()
        exit_code, output = run_command_in_guest(
            process_manager, vm, auth,
            f"echo {script_b64} | base64 -d > /tmp/grubfix.py", is_windows=False,
        )
        if exit_code != 0:
            logger.warning(f"couldn't write GRUB fix script to {vm.name}: {output}")
            return None

        exit_code, output = run_command_in_guest(
            process_manager, vm, auth,
            f"echo {password} | sudo -S python3 /tmp/grubfix.py", is_windows=False,
        )
        if exit_code != 0:
            logger.warning(f"GRUB fix script failed on {vm.name}: {output}")
            return None

        from src.settings import Settings

        _wait_for_task(vm.PowerOffVM_Task())
        _wait_for_task(vm.PowerOnVM_Task())

        port = _existing_serial_console(vm)
        if port is not None:
            _wait_until_port_open(Settings.ESXI.IP, port, _SERIAL_READY_TIMEOUT_SECONDS)
        return True
    except Exception as exc:  # best-effort - never break the caller's own flow
        logger.warning(f"guest console=ttyS0 setup skipped for {vm.name}: {exc!r}")
        return None


@router.websocket("/ws/vm-console/{node_name}")
async def vm_console_proxy(websocket: WebSocket, node_name: str) -> None:
    """
    Pure data-plane: just bridges bytes once the serial console is
    already added and settled. Never mutates the VM itself - that's a
    separate, explicit HTTP POST (see main.py's
    /vm-console/{node_name}/prepare), since adding the console for the
    first time can trigger a VM power cycle, which isn't something a
    WebSocket connection attempt should silently do as a side effect.
    Call prepare first; this closes with a clear reason if that was
    skipped.
    """
    await websocket.accept()
    app = websocket.app

    from src.graph.environment import Environment

    node = app.state.graph.nodes.get(node_name)
    if node is None or node.env != Environment.ON_ESXI:
        await websocket.close(code=1008, reason=f"no ESXi VM node named {node_name!r}")
        return

    vm = app.state.esxi_connection.get_vm(node_name)
    if vm is None:
        await websocket.close(code=1008, reason=f"VM {node_name!r} not found on ESXi")
        return

    port = _existing_serial_console(vm)
    if port is None:
        await websocket.close(
            code=1011,
            reason="serial console not prepared yet - call the prepare endpoint first",
        )
        return

    from src.settings import Settings

    try:
        reader, writer = await asyncio.open_connection(Settings.ESXI.IP, port)
    except OSError as exc:
        await websocket.close(code=1011, reason=f"couldn't reach serial console: {exc}")
        return

    await bridge(websocket, reader, writer, label=f"VM console proxy for {node_name!r}")
