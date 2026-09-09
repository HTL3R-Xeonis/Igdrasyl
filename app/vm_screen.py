"""
Embedded VM screens - the WebMKS ticket path is unreliable on this
project's real ESXi host (see HANDOFF.md: 6+ years of unpatched
bugfixes on a vCenter-managed 6.7U3 host, tunnel never completes past
TLS). This bypasses that entirely: ESXi's own VM process can serve
standard VNC/RFB directly (RemoteDisplay.vnc.* in extraConfig), with no
ticket, no rhttpproxy tunnel, no vCenter brokering involved at all -
just a plain TCP socket the VM's own vmx process listens on. Bridged to
a browser WebSocket the same way app/console.py bridges GNS3's Telnet
consoles; noVNC on the frontend speaks the RFB protocol directly to it.

Requires a one-time-per-VM host firewall opening (see HANDOFF.md for
the exact ruleset) and, if the VM is already powered on when VNC gets
enabled for the first time, a full power-off/power-on cycle to pick up
the new extraConfig - confirmed empirically that a plain reset leaves
the config correctly set but never actually starts the VNC listener;
only a fresh vmx process (a real power cycle, not a soft reset) does.
That power-cycle is a real, visible side effect of the first "open
screen" on a given VM - callers should surface this to the user before
triggering it, not just silently disrupt something they clicked a
screen button on.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

from fastapi import APIRouter, WebSocket
from loguru import logger

router = APIRouter()

_READ_CHUNK_SIZE = 4096
# Matches the vncserver ruleset opened on 10.20.20.202's firewall - see
# HANDOFF.md. A fixed, small pool (not a dynamic per-VM firewall rule)
# because opening a new port per VM would mean a firewall change per
# VM, which is a bigger ask than reusing a handful of pre-opened ports.
_VNC_PORT_RANGE = range(5900, 5911)
_TASK_POLL_INTERVAL_SECONDS = 0.5
_VNC_READY_TIMEOUT_SECONDS = 60
_EXPECTED_PUMP_EXCEPTIONS = (ConnectionResetError, ConnectionAbortedError)


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


def _existing_vnc_config(vm: Any) -> tuple[int, str] | None:
    config = {
        opt.key: opt.value
        for opt in (vm.config.extraConfig or [])
        if opt.key.startswith("RemoteDisplay.vnc.")
    }
    # ESXi echoes this value back as "TRUE" (uppercase) regardless of
    # the case written - confirmed empirically after this exact-match
    # check being case-sensitive made every prepare() call think VNC
    # was never configured, re-writing it (and power-cycling the VM)
    # on every single call instead of being idempotent after the first.
    enabled = (config.get("RemoteDisplay.vnc.enabled") or "").lower()
    if enabled == "true" and "RemoteDisplay.vnc.port" in config:
        return int(config["RemoteDisplay.vnc.port"]), config.get("RemoteDisplay.vnc.password", "")
    return None


def ensure_vnc_enabled(vm: Any, assigned_ports: dict[str, int]) -> tuple[int, str, bool]:
    """
    Returns (port, password, was_just_enabled). was_just_enabled is True
    only the first time this VM gets VNC turned on - the caller uses it
    to decide whether a disruptive power cycle is about to happen.
    :raises RuntimeError: if every port in _VNC_PORT_RANGE is already
        assigned to a different VM in this process's lifetime.
    """
    from pyVmomi import vim

    existing = _existing_vnc_config(vm)
    if existing is not None:
        return existing[0], existing[1], False

    used = set(assigned_ports.values())
    try:
        port = next(p for p in _VNC_PORT_RANGE if p not in used)
    except StopIteration:
        raise RuntimeError(
            f"no free VNC port left in {_VNC_PORT_RANGE.start}-{_VNC_PORT_RANGE.stop - 1}"
        )
    password = secrets.token_urlsafe(12)

    spec = vim.vm.ConfigSpec()
    spec.extraConfig = [
        vim.option.OptionValue(key="RemoteDisplay.vnc.enabled", value="true"),
        vim.option.OptionValue(key="RemoteDisplay.vnc.port", value=str(port)),
        vim.option.OptionValue(key="RemoteDisplay.vnc.password", value=password),
    ]
    _wait_for_task(vm.ReconfigVM_Task(spec))

    # Confirmed empirically against this project's real infrastructure
    # (see HANDOFF.md): a plain ResetVM_Task does NOT reliably make vmx
    # re-read extraConfig and start the VNC listener - only a full
    # power-off/power-on cycle (a new vmx process) does. A soft reset
    # left the config correctly set but nothing ever bound the port.
    if vm.runtime.powerState == vim.VirtualMachine.PowerState.poweredOn:
        logger.warning(f"Power-cycling {vm.name} to apply its first-time VNC config")
        _wait_for_task(vm.PowerOffVM_Task())
        _wait_for_task(vm.PowerOnVM_Task())

    from src.settings import Settings

    if not _wait_until_port_open(Settings.ESXI.IP, port, _VNC_READY_TIMEOUT_SECONDS):
        raise RuntimeError(
            f"VNC on {vm.name} didn't come up within {_VNC_READY_TIMEOUT_SECONDS}s "
            f"of the power cycle"
        )

    assigned_ports[vm.name] = port
    return port, password, True


async def _pump_tcp_to_ws(reader: asyncio.StreamReader, websocket: WebSocket) -> None:
    while True:
        data = await reader.read(_READ_CHUNK_SIZE)
        if not data:
            return
        await websocket.send_bytes(data)


async def _pump_ws_to_tcp(websocket: WebSocket, writer: asyncio.StreamWriter) -> None:
    while True:
        data = await websocket.receive_bytes()
        writer.write(data)
        await writer.drain()


@router.websocket("/ws/vm-screen/{node_name}")
async def vm_screen_proxy(websocket: WebSocket, node_name: str) -> None:
    """
    Pure data-plane: just bridges bytes once VNC is already enabled and
    settled. Never mutates the VM itself - that's a separate, explicit
    HTTP POST (see main.py's /vm-screen/{node_name}/prepare), since
    enabling VNC for the first time can trigger a VM reset, which isn't
    something a WebSocket connection attempt should silently do as a
    side effect. Call prepare first; this closes with a clear reason if
    that was skipped.
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

    existing = _existing_vnc_config(vm)
    if existing is None:
        await websocket.close(
            code=1011, reason="VNC not prepared yet - call the prepare endpoint first"
        )
        return
    port, _password = existing

    from src.settings import Settings

    try:
        reader, writer = await asyncio.open_connection(Settings.ESXI.IP, port)
    except OSError as exc:
        await websocket.close(code=1011, reason=f"couldn't reach VNC port: {exc}")
        return

    # RFB servers send a protocol-version line first, unprompted - the
    # frontend's noVNC client handles the full handshake (including the
    # password auth this VM was configured with) once bytes start
    # flowing, same as it would talking to a real VNC server directly.
    tcp_to_ws = asyncio.create_task(_pump_tcp_to_ws(reader, websocket))
    ws_to_tcp = asyncio.create_task(_pump_ws_to_tcp(websocket, writer))
    try:
        done, pending = await asyncio.wait(
            {tcp_to_ws, ws_to_tcp}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, _EXPECTED_PUMP_EXCEPTIONS):
                logger.debug(f"VM screen proxy for {node_name!r} ended with: {exc!r}")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        try:
            await websocket.close()
        except RuntimeError:
            pass
