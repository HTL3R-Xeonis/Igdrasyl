"""
Standalone spike: confirms whether WebMKS console access works against
this project's real ESXi infrastructure, before building any GUI around
it. Acquires a webmks ticket for a named, already-running VM and prints
the WebSocket URL webmks_test.html needs to render its console.

Uses AcquireTicket(ticketType="webmks") - the modern API. The older
AcquireMksTicket() is broken on ESXi 8 (see vmware/pyvmomi#1078) and
isn't used here at all.

Also self-heals a real bug found against this project's own lab host:
rhttpproxy's ticket-tunnel endpoint (/ticket -> /var/run/vmware/ticket/%1,
see /etc/vmware/rhttpproxy/endpoints.conf) silently black-holes every
WebMKS connection - TLS completes, ticket acquisition succeeds, but the
tunnel itself never sends a byte back - if that directory doesn't exist
on the actual ESXi host serving the ticket. Confirmed happening on this
lab's host after an ad-hoc hostd/rhttpproxy restart apparently never
recreated it (see TopologyOperator's HANDOFF.md for the full
investigation). ensure_ticket_directory() below checks/creates it via
SSH before printing the ticket URL, on whichever host actually ends up
serving the ticket - so this keeps self-healing automatically if the
same thing happens again here, or the first time it's hit against a
different host. It's a real, necessary fix but not a complete one on
this exact lab host: even with the directory present, the connection
still gets torn down one layer deeper (vmx's own mks subsystem logs
"owner closed connection in recv callback" the instant it's accepted) -
that part looks like a genuine host-build bug, not something fixable
from here (see HANDOFF.md - same conclusion as the classic-VNC bypass's
own dead end). Skipped entirely (with a warning, not a hard failure) if
no SSH password is given, or if SSH isn't reachable/enabled on the host -
this is a best-effort recovery attempt, not a hard requirement to
acquire a ticket.

Usage:
    python webmks_spike.py --host 10.20.20.202 --user root --password '...' --vm PC1
    # If the ESXi host's SSH login differs from the vSphere API password:
    python webmks_spike.py --host 10.20.20.202 --user root --password '...' \
        --vm PC1 --ssh-password '...'

--vm can be any already-running VM on the host - it doesn't need to be
part of a TopologyBuilder-deployed topology, this only tests whether
WebMKS itself works against this infrastructure.
"""

from __future__ import annotations

import argparse
import ssl
import sys

import paramiko
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim


def ensure_ticket_directory(host: str, ssh_user: str, ssh_password: str | None) -> None:
    """
    Idempotent: safe to call every time, on any host. Best-effort - never
    raises, since a missing/unreachable SSH login just means this self-heal
    step can't run, not that ticket acquisition itself should fail.
    """
    if not ssh_password:
        print(
            f"No SSH password given - skipping ticket-directory self-heal check on {host}."
        )
        return
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(host, username=ssh_user, password=ssh_password, timeout=8)
        try:
            _, stdout, stderr = client.exec_command("mkdir -p /var/run/vmware/ticket")
            if stdout.channel.recv_exit_status() != 0:
                print(
                    f"Warning: couldn't ensure ticket directory on {host}: "
                    f"{stderr.read().decode(errors='replace').strip()}"
                )
        finally:
            client.close()
    except Exception as exc:
        print(
            f"Warning: ticket-directory self-heal check skipped for {host} "
            f"({type(exc).__name__}: {exc})"
        )


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
    parser.add_argument(
        "--ssh-user", default="root", help="SSH login for the self-heal check"
    )
    parser.add_argument(
        "--ssh-password",
        default=None,
        help="SSH password for the ticket-directory self-heal check (see module "
        "docstring) - defaults to skipping that check if not given",
    )
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

        # ticket.host can come back empty when talking directly to a
        # standalone ESXi host - fall back to the host we connected to in
        # that case. But when going through vCenter (this host reported
        # itself as vCenter-managed), ticket.host is the correct, real
        # ESXi host to open the console on - which can genuinely differ
        # from vCenter's own address, so it must NOT be overridden here.
        ticket_host = ticket.host if ticket.host else args.host
        ws_url = f"wss://{ticket_host}:{ticket.port}/ticket/{ticket.ticket}"

        ensure_ticket_directory(ticket_host, args.ssh_user, args.ssh_password)

        print("WebMKS ticket acquired successfully.\n")
        print(f"  ticket.host (as returned): {getattr(ticket, 'host', None)!r}")
        print(f"  ticket.port: {ticket.port}")
        print(f"  ticket.ticket: {ticket.ticket}")
        print(f"  host used in the URL: {ticket_host}")
        print(f"\n  WebSocket URL to use: {ws_url}\n")
        print("Paste that URL into novnc_test.html's input field and click Connect.")
        return 0
    finally:
        Disconnect(service_instance)


if __name__ == "__main__":
    sys.exit(main())
