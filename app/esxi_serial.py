"""
Network-backed serial ports for ESXi VMs - what app/serial_agent.py talks to.

A VM gets a virtual serial port whose backing is a TCP *server* on the ESXi
host (`tcp://:<port>`): whatever the guest writes to its serial line shows up
for the first client that connects to <esxi host>:<port>, and what the client
sends goes into the guest. Plain TCP, no tickets and no tunnel - this is the
same mechanism the earlier (since removed) interactive serial console used, and
the same facts hold, all confirmed live against this project's ESXi host:

* the host firewall ruleset `remoteSerialPort` has to be enabled (once per host,
  done here through the API; it covers inbound TCP 23 and 1024-65535),
* adding a serial port to a running VM fails (InvalidPowerState), so the VM is
  powered off, changed and powered on again - a brief, deliberate interruption,
* the listener only exists while the VM is powered on, and only a real power
  cycle (not a guest reboot) makes the virtual UART attach to it.

Ports come from a small pool (2000-2010); which ones are taken is read from the
VMs' own devices every time, so nothing is remembered across restarts.

For Linux guests with VMware Tools the guest side is set up too: a login prompt
on the serial line (`serial-getty@ttyS0`). Appliances (MikroTik, pfSense,
OPNsense) bring their own serial console - on those only the hardware is added.
"""

from __future__ import annotations

import re
import socket
import time
from typing import Any

from fastapi import FastAPI
from loguru import logger
from src.settings import Settings

from app.guest_ops import guest_credentials_for_image, run_command_in_guest
from app.live_lock import exclusive

SERIAL_PORT_RANGE = range(2000, 2011)
_FIREWALL_RULESET = "remoteSerialPort"
_URI = re.compile(r"^tcp://(?::(\d+))?$")
_TASK_POLL_SECONDS = 0.5
_PORT_OPEN_TIMEOUT_SECONDS = 90
_TOOLS_TIMEOUT_SECONDS = 180


class SerialSetupError(Exception):
    """The serial console could not be set up; the message is for the operator."""


def serial_port_of(vm: Any) -> int | None:
    """The TCP port of the VM's network serial port on the ESXi host, None if it has none."""
    from pyVmomi import vim

    for device in vm.config.hardware.device:
        if not isinstance(device, vim.vm.device.VirtualSerialPort):
            continue
        backing = device.backing
        if not isinstance(backing, vim.vm.device.VirtualSerialPort.URIBackingInfo):
            continue
        match = _URI.match(backing.serviceURI or "")
        if match and match.group(1):
            return int(match.group(1))
    return None


def _wait_for_task(task: Any) -> None:
    from pyVmomi import vim

    while task.info.state not in (vim.TaskInfo.State.success, vim.TaskInfo.State.error):
        time.sleep(_TASK_POLL_SECONDS)
    if task.info.state == vim.TaskInfo.State.error:
        raise SerialSetupError(str(task.info.error))


def _port_open(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=3):
                return True
        except OSError:
            time.sleep(2)
    return False


def _free_port(esxi_connection: Any) -> int:
    taken = {
        port
        for vm in esxi_connection.get_all_vms()
        if (port := serial_port_of(vm)) is not None
    }
    for port in SERIAL_PORT_RANGE:
        if port not in taken:
            return port
    raise SerialSetupError(
        f"every serial port in {SERIAL_PORT_RANGE.start}-{SERIAL_PORT_RANGE.stop - 1} is already used by a VM"
    )


def status(app: FastAPI, name: str) -> dict[str, Any]:
    """{"enabled": bool, "port": int | None, "tools": str, "powered_on": bool} for an ESXi VM node."""
    vm = _vm(app, name)
    port = serial_port_of(vm)
    return {
        "enabled": port is not None,
        "port": port,
        "tools": str(vm.guest.toolsRunningStatus),
        "powered_on": str(vm.runtime.powerState) == "poweredOn",
    }


def _vm(app: FastAPI, name: str) -> Any:
    from src.graph.environment import Environment

    node = app.state.graph.nodes.get(name) if app.state.graph is not None else None
    if node is None or node.env != Environment.ON_ESXI:
        raise SerialSetupError(f"{name!r} is not an ESXi VM node of this topology")
    vm = app.state.esxi_connection.get_vm(name)
    if vm is None:
        raise SerialSetupError(f"VM {name!r} was not found on the ESXi host")
    return vm


def enable(app: FastAPI, name: str, setup_guest: bool = True) -> dict[str, Any]:
    """
    Gives the VM a network serial port (briefly powering it off and on again when it
    runs) and, for a Linux guest with VMware Tools, a login prompt on it.
    :raises SerialSetupError: not an ESXi VM, no free port, or ESXi refused
    """
    with exclusive("Enable serial console"):
        return _enable(app, name, setup_guest)


def _enable(app: FastAPI, name: str, setup_guest: bool) -> dict[str, Any]:
    from pyVmomi import vim

    esxi = app.state.esxi_connection
    vm = _vm(app, name)
    existing = serial_port_of(vm)
    if existing is not None:
        return {
            "enabled": True,
            "port": existing,
            "changed": False,
            "guest": "unchanged",
        }

    port = _free_port(esxi)
    logger.warning(f"Adding a network serial port ({port}) to VM {name!r}")
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

    was_on = vm.runtime.powerState == vim.VirtualMachine.PowerState.poweredOn
    if was_on:
        _wait_for_task(vm.PowerOffVM_Task())
    _wait_for_task(vm.ReconfigVM_Task(vim.vm.ConfigSpec(deviceChange=[device_spec])))
    if was_on:
        _wait_for_task(vm.PowerOnVM_Task())
        if not _port_open(Settings.ESXI.IP, port, _PORT_OPEN_TIMEOUT_SECONDS):
            raise SerialSetupError(
                f"the serial port {port} of {name!r} did not open within "
                f"{_PORT_OPEN_TIMEOUT_SECONDS}s after being added"
            )

    guest = "not attempted"
    if setup_guest and was_on:
        guest = _setup_guest_login(app, vm, name)
    return {"enabled": True, "port": port, "changed": True, "guest": guest}


def _setup_guest_login(app: FastAPI, vm: Any, name: str) -> str:
    """
    Linux with VMware Tools: start a login prompt on ttyS0 so the serial line
    is usable. Best effort - the text says what happened, it never raises.
    """
    from pyVmomi import vim

    node = app.state.graph.nodes[name]
    if "windows" in node.image.lower():
        return "skipped (Windows has no serial login)"
    deadline = time.monotonic() + _TOOLS_TIMEOUT_SECONDS
    while str(vm.guest.toolsRunningStatus) != "guestToolsRunning":
        if time.monotonic() > deadline:
            return "skipped (VMware Tools is not running - the guest has to provide its own serial login)"
        time.sleep(3)
    try:
        creds = guest_credentials_for_image(node.image)
        if creds is None:
            return "skipped (no guest credentials in the template catalog)"
        auth = vim.vm.guest.NamePasswordAuthentication(
            username=creds[0], password=creds[1]
        )
        process_manager = (
            app.state.esxi_connection.content.guestOperationsManager.processManager
        )
        code, output = run_command_in_guest(
            process_manager,
            vm,
            auth,
            "systemctl enable --now serial-getty@ttyS0.service",
            is_windows=False,
        )
        if code == 0:
            return "serial login enabled in the guest"
        return f"skipped (the guest refused: {output})"
    except Exception as exc:
        return f"skipped ({type(exc).__name__}: {exc})"
