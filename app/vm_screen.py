"""
Embedded VM screens via WebMKS (vSphere's own built-in console-ticket
API), proxied through this app's own backend rather than connected to
directly from the browser: a WebMKS ticket is short-lived/effectively
single-use, and the ESXi host's TLS cert is self-signed (a browser
would refuse it outright, or an operator would have to manually trust
it per-host) - both sidestepped by acquiring the ticket and opening the
real upstream WebSocket server-side, right when a browser connects,
then just bridging bytes (see app/ws_bridge.py). Same "proxy through
our own backend" shape as app/console.py, just with a WebSocket-to-
WebSocket bridge instead of TCP-to-WebSocket, since WebMKS's own
transport already is a WebSocket carrying raw RFB (VNC) protocol bytes.

Historically a dead end on this project's real infrastructure (see
HANDOFF.md's full investigation): WebMKS and classic VNC both hit the
exact same "accepted then instantly closed, zero bytes" failure,
traced to what looked like a genuine build-level bug in vmx's own mks
subsystem - concluded as infrastructure work beyond this project's own
scope, and parked in favor of a network-backed serial console instead
(since removed - this Screen feature alone now covers ESXi VM access).
Re-verified fresh, independently, before building any of this - and
found now genuinely working end-to-end: a full RFB handshake
completes, with a real framebuffer and desktop name back from the
actual VM. Whatever was broken on the host appears to have been
resolved since the last investigation (unconfirmed why - possibly a
host patch, a reboot, or a hostd/rhttpproxy restart that happened for
unrelated reasons).

This needs no VM reconfiguration and no power cycle - WebMKS works for
any already-running VM regardless of guest OS, so there's no separate
"prepare" endpoint here (contrast the removed serial console's own
prepare step, which could take up to ~60s and power-cycled the VM the
first time). The only precondition is that the VM is actually powered
on, checked before ever acquiring a ticket.
"""

from __future__ import annotations

import ssl

import websockets
from fastapi import APIRouter, WebSocket
from loguru import logger

from app.ws_bridge import bridge_ws

router = APIRouter()

# Same self-signed-cert handling ESXiConnection.connect() already uses -
# not a shortcut specific to this module, this host's cert is genuinely
# self-signed.
_UNVERIFIED_SSL_CONTEXT = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
_UNVERIFIED_SSL_CONTEXT.check_hostname = False
_UNVERIFIED_SSL_CONTEXT.verify_mode = ssl.CERT_NONE

_UPSTREAM_CONNECT_TIMEOUT_SECONDS = 10


@router.websocket("/ws/vm-screen/{node_name}")
async def vm_screen_proxy(websocket: WebSocket, node_name: str) -> None:
    """
    Accepts the browser's WebSocket (with the "binary" subprotocol
    noVNC's RFB.js requests), acquires a fresh WebMKS ticket for
    node_name, opens the real upstream WebMKS WebSocket, then bridges
    raw RFB bytes between the two until either side closes. Refuses
    - with a clear close reason surfaced to the frontend - if the node
    isn't a running ESXi VM, or if a ticket/upstream connection can't
    be obtained.
    """
    await websocket.accept(subprotocol="binary")
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

    from pyVmomi import vim

    if vm.runtime.powerState != vim.VirtualMachine.PowerState.poweredOn:
        await websocket.close(
            code=1008,
            reason=(
                f"VM {node_name!r} must be powered on for a screen "
                f"(status: {vm.runtime.powerState})"
            ),
        )
        return

    try:
        ticket = vm.AcquireTicket(ticketType="webmks")
    except Exception as exc:
        logger.warning(f"WebMKS ticket acquisition failed for {node_name!r}: {exc!r}")
        await websocket.close(code=1011, reason=f"couldn't acquire a WebMKS ticket: {exc}")
        return

    from src.settings import Settings

    # ticket.host can come back empty when talking directly to a
    # standalone ESXi host - falls back to our own configured ESXi IP in
    # that case. When going through vCenter (this project's real host
    # is vCenter-managed), ticket.host is the correct, real ESXi host to
    # open the console on, which must NOT be overridden.
    ticket_host = ticket.host or Settings.ESXI.IP
    ws_url = f"wss://{ticket_host}:{ticket.port}/ticket/{ticket.ticket}"

    try:
        upstream = await websockets.connect(
            ws_url,
            subprotocols=["binary"],
            ssl=_UNVERIFIED_SSL_CONTEXT,
            open_timeout=_UPSTREAM_CONNECT_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning(f"couldn't reach WebMKS console for {node_name!r}: {exc!r}")
        await websocket.close(code=1011, reason=f"couldn't reach WebMKS console: {exc}")
        return

    await bridge_ws(websocket, upstream, label=f"VM screen proxy for {node_name!r}")
