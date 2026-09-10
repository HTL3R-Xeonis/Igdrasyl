"""
Shared pyvmomi Guest Operations helpers - credential lookup and one-
command-at-a-time execution, used by both app/config_push.py (pushing a
device's raw command list) and app/vm_console.py (the one-off guest-side
console=ttyS0 setup). Factored out here once a second consumer needed
the exact same two things, same reasoning as app/tcp_bridge.py.
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
    process couldn't even start (a MethodFault, e.g. bad credentials)
    or timed out, with output describing which.
    """
    from pyVmomi import vim

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
        return None, str(exc)

    exit_code = wait_for_guest_process(process_manager, vm, auth, pid)
    output = "timed out waiting for exit" if exit_code is None else f"exit code {exit_code}"
    return exit_code, output
