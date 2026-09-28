"""
Shared pyvmomi Guest Operations helpers - credential lookup and one-
command-at-a-time execution, used by app/config_push.py (pushing a
device's raw command list). Originally factored out once a second
consumer (a now-removed ESXi VM serial-console feature's one-off
guest-side console=ttyS0 setup) needed the exact same two things, same
reasoning as app/tcp_bridge.py.
"""

from __future__ import annotations

import time
from typing import Any

from src.connections.api_handler import APIHandler
from src.graph.environment import normalize_template_name
from src.settings import Settings

_GUEST_OPS_TIMEOUT_SECONDS = 60
_GUEST_OPS_POLL_INTERVAL_SECONDS = 1.0


def guest_credentials_for_image(image: str) -> tuple[str, str] | None:
    """
    The `root` user/password pair (parallel lists, index-matched) from
    the ESXi Template-API's per-template metadata for the given image,
    falling back to the first pair if no `root` entry exists (e.g. the
    Windows templates, which only ever list one non-root account) - see
    HANDOFF.md, confirmed live on this project's real infrastructure
    (e.g. debian: users=[junioradmin, root], passwords=[junioradmin,
    REMOVED]). Real bug found live: the first pair (junioradmin on
    every Linux template) has no passwordless sudo configured, so
    config-push commands needing root - i.e. almost anything that
    actually configures the guest - failed silently (guest-ops only
    ever reports an exit code, never *why* a command failed). Mirrors
    APIHandler.find_esxi_template_file's own lenient-name-match pattern
    rather than adding a new endpoint.
    """
    data = APIHandler.get(f"{Settings.API.ESXI_TEMPLATE_SERVER_URL}/api/templates")
    normalized = normalize_template_name(image)
    for template in data["templates"]:
        if normalize_template_name(template["name"]) != normalized:
            continue
        users = template.get("users") or []
        passwords = template.get("passwords") or []
        if not users or not passwords:
            return None
        for user, password in zip(users, passwords):
            if user.lower() == "root":
                return user, password
        return users[0], passwords[0]
    return None


def wait_for_guest_process(process_manager, vm, auth, pid: int) -> int | None:
    """Polls until the guest process exits, or returns None on timeout."""
    deadline = time.monotonic() + _GUEST_OPS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        procs = process_manager.ListProcessesInGuest(vm, auth, [pid])
        if procs and procs[0].endTime is not None:
            return procs[0].exitCode
        time.sleep(_GUEST_OPS_POLL_INTERVAL_SECONDS)
    return None


def run_command_in_guest(
    process_manager: Any, vm: Any, auth: Any, command: str, is_windows: bool
) -> tuple[int | None, str]:
    """
    Runs one command to completion via guest-ops, waiting for its exit
    code. Returns (exit_code, output) - exit_code is None if the
    process couldn't even start (bad credentials, guest ops unavailable,
    or a timeout), with output describing which.
    """
    from pyVmomi import vim, vmodl

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
    except vmodl.MethodFault as exc:
        # Real, pre-existing bug found live (an OPNsense VM with no
        # VMware Tools installed hit this): the base vSphere API fault
        # class is `vmodl.MethodFault`, not `vim.fault.MethodFault` -
        # `vim.fault` only holds specific subtypes (InvalidLogin,
        # NoPermission, ...), not the base class itself. The original
        # `except vim.fault.MethodFault` here could never actually
        # match anything - referencing that attribute at all raises its
        # own `AttributeError('MethodFault')` from pyvmomi's lazy
        # WSDL-type lookup (confirmed directly: `hasattr(vim.fault,
        # "MethodFault")` is False in this pyvmomi version), which is
        # exactly the cryptic error this project's own logs showed
        # instead of a clean "guest ops failed" message.
        return None, str(exc)
    except Exception as exc:
        # Still a safety net beneath the fix above - any other failure
        # (not a vmodl.MethodFault at all) still returns cleanly rather
        # than breaking this function's own documented "always returns
        # (None, reason), never raises" contract.
        return None, f"{type(exc).__name__}: {exc}"

    exit_code = wait_for_guest_process(process_manager, vm, auth, pid)
    output = (
        "timed out waiting for exit" if exit_code is None else f"exit code {exit_code}"
    )
    return exit_code, output
