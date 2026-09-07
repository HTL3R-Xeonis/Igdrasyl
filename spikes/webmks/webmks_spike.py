"""
Standalone spike: confirms whether WebMKS console access works against
this project's real ESXi infrastructure, before building any GUI around
it. Acquires a webmks ticket for a named, already-running VM and prints
the WebSocket URL webmks_test.html needs to render its console.

Uses AcquireTicket(ticketType="webmks") - the modern API. The older
AcquireMksTicket() is broken on ESXi 8 (see vmware/pyvmomi#1078) and
isn't used here at all.

Usage:
    python webmks_spike.py --host 10.20.20.202 --user root --password '...' --vm PC1

--vm can be any already-running VM on the host - it doesn't need to be
part of a TopologyBuilder-deployed topology, this only tests whether
WebMKS itself works against this infrastructure.
"""

from __future__ import annotations

import argparse
import ssl
import sys

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim


def find_vm(content, name: str):
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    try:
        for vm in container.view:
            if vm.name == name:
                return vm
    finally:
        container.Destroy()
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="ESXi host IP/hostname")
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--vm", required=True, help="Name of an already-running VM")
    args = parser.parse_args()

    # Self-signed ESXi cert - same approach TopologyBuilder's own
    # ESXiConnection already uses, not a shortcut specific to this spike.
    ssl_context = ssl._create_unverified_context()

    service_instance = SmartConnect(
        host=args.host,
        port=args.port,
        user=args.user,
        pwd=args.password,
        sslContext=ssl_context,
    )
    try:
        content = service_instance.RetrieveContent()
        vm = find_vm(content, args.vm)
        if vm is None:
            print(f"VM '{args.vm}' not found on {args.host}", file=sys.stderr)
            return 1
        if vm.runtime.powerState != "poweredOn":
            print(
                f"VM '{args.vm}' is not powered on "
                f"(state: {vm.runtime.powerState}) - power it on first.",
                file=sys.stderr,
            )
            return 1

        ticket = vm.AcquireTicket(ticketType="webmks")

        # Known gotcha: ticket.host can come back wrong/unreachable on a
        # standalone ESXi host - use the host we actually connected to
        # instead of trusting that field.
        ws_url = f"wss://{args.host}:{ticket.port}/ticket/{ticket.ticket}"

        print("WebMKS ticket acquired successfully.\n")
        print(f"  ticket.host (as returned by ESXi, may be wrong): {getattr(ticket, 'host', None)}")
        print(f"  ticket.port: {ticket.port}")
        print(f"  ticket.ticket: {ticket.ticket}")
        print(f"\n  WebSocket URL to use: {ws_url}\n")
        print("Paste that URL into webmks_test.html's input field and click Connect.")
        return 0
    finally:
        Disconnect(service_instance)


if __name__ == "__main__":
    sys.exit(main())
