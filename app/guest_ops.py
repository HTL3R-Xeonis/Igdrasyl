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


_MAX_OUTPUT_CHARS = 20_000
_GUEST_FILE_TRANSFER_TIMEOUT_SECONDS = 20


def _quote_for_bash(command: str) -> str:
    """Single-quotes the command for `bash -c`, so quotes/$/spaces in it reach bash untouched."""
    return "'" + command.replace("'", "'\\''") + "'"


def _read_and_delete_guest_file(
    file_manager: Any, vm: Any, auth: Any, guest_path: str, esxi_host: str
) -> str | None:
    """
    Downloads a text file out of the guest through ESXi's guest file transfer
    (an HTTPS URL it hands out) and deletes it in the guest afterwards.
    Best effort: None if anything fails - the caller then just reports the
    exit code, which is all guest operations offer by themselves.
    """
    import requests

    try:
        info = file_manager.InitiateFileTransferFromGuest(vm, auth, guest_path)
        # ESXi writes "*" where its own hostname belongs.
        url = info.url.replace("*", esxi_host, 1)
        response = requests.get(
            url, verify=False, timeout=_GUEST_FILE_TRANSFER_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        text = response.content.decode("utf-8", errors="replace")
    except Exception:
        return None
    finally:
        try:
            file_manager.DeleteFileInGuest(vm, auth, guest_path)
        except Exception:
            pass
    return text[:_MAX_OUTPUT_CHARS]


def run_command_in_guest(
    process_manager: Any,
    vm: Any,
    auth: Any,
    command: str,
    is_windows: bool,
    file_manager: Any = None,
    esxi_host: str | None = None,
) -> tuple[int | None, str]:
    """
    Runs one command to completion via guest-ops, waiting for its exit
    code. Returns (exit_code, output) - exit_code is None if the
    process couldn't even start (bad credentials, guest ops unavailable,
    or a timeout), with output describing which.

    Guest operations only ever report an exit code. With a `file_manager`
    (and the ESXi host's address) the command's own stdout/stderr is
    redirected to a temporary file in the guest, fetched through ESXi's guest
    file transfer and deleted again, so the output is the command's real text
    (at most 20 000 characters); if that fails the output is just
    "exit code N". The command is passed to the guest quoted, so it may
    contain quotes, $variables and pipes.
    """
    import uuid

    from pyVmomi import vim, vmodl

    capture = file_manager is not None and esxi_host is not None
    token = uuid.uuid4().hex[:12]
    if is_windows:
        guest_file = f"C:\\Windows\\Temp\\topo-push-{token}.out"
        arguments = (
            f'/c ({command}) > "{guest_file}" 2>&1' if capture else f"/c {command}"
        )
        spec = vim.vm.guest.ProcessManager.ProgramSpec(
            programPath="C:\\Windows\\System32\\cmd.exe", arguments=arguments
        )
    else:
        guest_file = f"/tmp/topo-push-{token}.out"
        script = f"{{ {command}\n}} > {guest_file} 2>&1" if capture else command
        spec = vim.vm.guest.ProcessManager.ProgramSpec(
            programPath="/bin/bash", arguments=f"-c {_quote_for_bash(script)}"
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
    if exit_code is None:
        return None, "timed out waiting for exit"
    if capture:
        text = _read_and_delete_guest_file(
            file_manager, vm, auth, guest_file, esxi_host
        )
        if text is not None:
            return exit_code, text if text.strip() else f"exit code {exit_code}"
    return exit_code, f"exit code {exit_code}"
