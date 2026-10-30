"""
Directives inside a device's command list - lines that are not sent to the device but steer the push.

    @reboot [seconds]        restart the device now, wait until it is back, carry on with the next commands
    @wait-reboot [seconds]   a command before this one restarts the device by itself (promoting a domain
                             controller does); wait until it went down and came back

`seconds` is the longest wait for the device to be back (default 900). Any other line, also one that merely
starts with "@" (a Windows `@echo off`), is an ordinary command.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

DEFAULT_TIMEOUT_SECONDS = 900
# a restart that was asked for (or announced by the previous command) has to start this quickly
_WENT_DOWN_WITHIN_SECONDS = 300
_POLL_SECONDS = 3

_DIRECTIVE = re.compile(r"^@(reboot|wait-reboot)(?:\s+(\d+))?$", re.IGNORECASE)


class RebootError(Exception):
    """The device did not go down or did not come back; the message is for the operator."""


@dataclass(frozen=True)
class Directive:
    kind: Literal["reboot", "wait-reboot"]
    timeout: int


def parse(command: str) -> Directive | None:
    """The directive a command line stands for, None for an ordinary command."""
    match = _DIRECTIVE.match(command.strip())
    if match is None:
        return None
    timeout = int(match.group(2)) if match.group(2) else DEFAULT_TIMEOUT_SECONDS
    return Directive(kind=match.group(1).lower(), timeout=timeout)  # type: ignore[arg-type]


def has_directive(commands: list[str]) -> bool:
    return any(parse(command) is not None for command in commands)


def _guest_is_up(vm: Any) -> bool:
    return str(vm.guest.toolsRunningStatus) == "guestToolsRunning" and bool(
        vm.guest.guestOperationsReady
    )


def wait_for_guest_reboot(
    vm: Any,
    timeout: int,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """
    Waits until the VM's guest operations went away and are back (VMware Tools running again).
    Returns a short text for the result, raises RebootError.
    """
    start = clock()
    deadline = start + timeout
    must_be_down_by = start + min(timeout, _WENT_DOWN_WITHIN_SECONDS)
    while _guest_is_up(vm):
        if clock() >= must_be_down_by:
            raise RebootError(
                f"the VM did not restart within {min(timeout, _WENT_DOWN_WITHIN_SECONDS)}s"
            )
        sleep(_POLL_SECONDS)
    while not _guest_is_up(vm):
        if clock() >= deadline:
            raise RebootError(f"the VM was not back within {timeout}s")
        sleep(_POLL_SECONDS)
    return f"restarted, back after {clock() - start:.0f}s"
