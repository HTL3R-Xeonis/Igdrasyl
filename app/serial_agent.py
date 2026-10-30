"""
The serial-console agent: logs in on a VM's serial console and runs a list of
commands there, like an operator typing - for VMs that guest operations cannot
reach (no VMware Tools: MikroTik, pfSense, OPNsense, any appliance), or when a
console is simply wanted.

How it works: a small expect engine over a plain TCP socket (the ESXi host's
network serial port, see app/esxi_serial.py). It wakes the console, recognises
what is on the screen - login prompt, password prompt, a console menu, a shell
prompt - answers it, and then runs the commands one at a time:

* "marker" profiles (Linux, FreeBSD shells): after each command the agent has the
  shell print a unique end marker with the command's exit status, so it knows
  exactly where the output ends and whether the command succeeded.
* "prompt" profiles (RouterOS): a command is finished when the CLI prompt is back;
  there is no exit status, so failure is recognised from the CLI's error texts.

Profiles carry everything that differs per system (prompts, login suffix, logout
command, error texts). The agent never logs passwords, commands or console
output - only counts and the reason of a failure.

Limits, on purpose: Windows (the Special Administration Console) is not
supported; a console that is stuck in a full-screen program (an editor, a pager,
a boot menu) times out instead of being guessed at.
"""

from __future__ import annotations

import re
import select
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Self

from loguru import logger

from app import push_directives

_ANSI = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>78MDEHc]|\x1b\][^\x07]*\x07"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

DEFAULT_COMMAND_TIMEOUT_SECONDS = 60
LOGIN_TIMEOUT_SECONDS = 90
_QUIET_RETRY_SECONDS = 3.0
_FIRST_QUIET_SECONDS = 1.5
_POST_LOGIN_QUIET_SECONDS = 0.6
# a device that just booted keeps printing (log lines, redrawn prompts) for a while
_POST_REBOOT_QUIET_SECONDS = 2.0
_MAX_OUTPUT_CHARS = 20_000
_MAX_BUFFER_CHARS = 200_000


class AgentError(Exception):
    """Base class: the console could not be driven; the message is for the operator."""


class AgentConnectionError(AgentError):
    """The serial port could not be reached (not enabled, wrong port, in use)."""


class LoginFailed(AgentError):
    """The console rejected the credentials."""


class AgentTimeout(AgentError):
    """The console did not show what was expected in time."""


@dataclass(frozen=True)
class Profile:
    name: str
    login_prompt: re.Pattern[str]
    password_prompt: re.Pattern[str]
    shell_prompt: re.Pattern[str]
    # "marker": the shell prints an end marker + exit status; "prompt": the CLI
    # prompt ends a command and errors are recognised from the output text.
    style: str = "marker"
    # Appended to the user name at login (RouterOS: "+ct200w" = no colours, no
    # terminal auto-detection, wide lines).
    login_suffix: str = ""
    # A console menu that has to be answered before a shell exists (pfSense and
    # OPNsense: "8" opens a shell).
    menu_prompt: re.Pattern[str] | None = None
    menu_shell_choice: str = "8"
    # Typed once in the shell before the first command (turn echo off, ...).
    shell_setup: tuple[str, ...] = ()
    logout_command: str = "exit"
    # What restarts the device from its prompt, and the answer to its "are you sure"
    # question (RouterOS asks). None: the console cannot restart the device itself.
    reboot_command: str | None = None
    reboot_confirm: str | None = None
    login_failed: re.Pattern[str] = re.compile(
        r"login incorrect|authentication fail|invalid password|login failed",
        re.IGNORECASE,
    )
    error_markers: tuple[str, ...] = ()


_LOGIN = re.compile(r"(?im)(^|\n|\r)[^\n\r]*\blogin:\s*$")
_PASSWORD = re.compile(r"(?i)password:\s*$")

PROFILES: dict[str, Profile] = {
    "linux": Profile(
        name="linux",
        login_prompt=_LOGIN,
        password_prompt=_PASSWORD,
        shell_prompt=re.compile(r"[#$%>] ?$"),
        shell_setup=(
            (
                "bind 'set enable-bracketed-paste off' 2>/dev/null; "
                "stty -echo 2>/dev/null; export PS1='' PS2='' PROMPT_COMMAND=''"
            ),
        ),
        reboot_command="reboot",
    ),
    "bsd_menu": Profile(
        name="bsd_menu",
        login_prompt=_LOGIN,
        password_prompt=_PASSWORD,
        # "# " / "$ ", or pfSense's "[2.7.2-RELEASE][root@pfSense]/root: "
        shell_prompt=re.compile(r"(?:[#$%] ?|\][^\n\[\]]*: ?)$"),
        menu_prompt=re.compile(r"Enter an option:\s*$"),
        menu_shell_choice="8",
        shell_setup=("sh", "stty -echo 2>/dev/null; PS1='' ; export PS1"),
        logout_command="exit\rexit",
    ),
    "routeros": Profile(
        name="routeros",
        login_prompt=re.compile(r"(?i)\blogin:\s*$"),
        password_prompt=_PASSWORD,
        shell_prompt=re.compile(r"\[[^\]\n]+\] ?> ?$"),
        style="prompt",
        login_suffix="+ct200w",
        logout_command="/quit",
        reboot_command="/system reboot",
        reboot_confirm="y",
        error_markers=(
            "failure:",
            "bad command name",
            "syntax error",
            "expected end of command",
            "no such item",
            "invalid value",
            "input does not match",
            "ambiguous",
            "invalid internal item number",
            "not enough permissions",
        ),
    ),
}


def profile_for_image(image: str) -> Profile | None:
    """The default profile for a template image, None where serial is not supported."""
    lowered = image.lower()
    if "windows" in lowered:
        return None
    if "mikrotik" in lowered or "routeros" in lowered:
        return PROFILES["routeros"]
    if "pfsense" in lowered or "opnsense" in lowered:
        return PROFILES["bsd_menu"]
    return PROFILES["linux"]


def _split_partial_escape(text: str) -> tuple[str, str]:
    """(complete text, an unfinished escape sequence at its very end to be joined with the next packet)."""
    start = text.rfind("\x1b")
    if start == -1 or start < len(text) - 32:
        return text, ""
    tail = text[start:]
    if _ANSI.match(tail) and _ANSI.match(tail).end() == len(tail):
        return text, ""
    if len(tail) >= 2 and not tail[1] in "[(]":
        return text, ""  # a two-character sequence is complete
    if _ANSI.match(tail):
        return text, ""  # complete, followed by more text
    return text[:start], tail


def clean(text: str) -> str:
    """Console text without terminal escape sequences and control characters, newlines normalised."""
    text = _ANSI.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL.sub("", text)


@dataclass
class SerialAgent:
    """One logged-in conversation with one serial console. Use as a context manager."""

    host: str
    port: int
    profile: Profile
    username: str
    password: str
    connect_timeout: float = 10
    sleep: Callable[[float], None] = time.sleep
    _sock: socket.socket | None = field(default=None, init=False, repr=False)
    _buffer: str = field(default="", init=False, repr=False)
    _nonce: str = field(default_factory=lambda: uuid.uuid4().hex[:8], init=False)
    # An escape sequence cut in two by the end of a network packet waits here for its rest.
    _partial_escape: str = field(default="", init=False, repr=False)
    logged_in: bool = field(default=False, init=False)

    # ---- connection ----

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> None:
        try:
            self._sock = socket.create_connection(
                (self.host, self.port), timeout=self.connect_timeout
            )
        except OSError as exc:
            raise AgentConnectionError(
                f"can't reach the serial console at {self.host}:{self.port} ({exc}) - "
                f"is the serial port enabled for this VM, and not in use by another session?"
            ) from exc
        self._sock.setblocking(False)

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            if self.logged_in:
                self._send(self.profile.logout_command + "\r", sock)
                time.sleep(0.3)
        except OSError:
            pass
        finally:
            self.logged_in = False
            try:
                sock.close()
            except OSError:
                pass

    # ---- low level ----

    def _send(self, text: str, sock: socket.socket | None = None) -> None:
        target = sock or self._sock
        if target is None:
            raise AgentConnectionError("the serial console connection is closed")
        data = text.encode("utf-8", errors="replace")
        deadline = time.monotonic() + 10
        while data:
            _, writable, _ = select.select([], [target], [], 1)
            if writable:
                sent = target.send(data)
                data = data[sent:]
            elif time.monotonic() > deadline:
                raise AgentTimeout("the serial console does not accept input")

    def _read(self, timeout: float) -> bool:
        """Reads what is available within `timeout` into the buffer. False if nothing came."""
        if self._sock is None:
            raise AgentConnectionError("the serial console connection is closed")
        readable, _, _ = select.select([self._sock], [], [], timeout)
        if not readable:
            return False
        try:
            chunk = self._sock.recv(65536)
        except BlockingIOError:
            return False
        except OSError as exc:
            raise AgentConnectionError(
                f"the serial console connection broke: {exc}"
            ) from exc
        if not chunk:
            raise AgentConnectionError("the serial console closed the connection")
        text, self._partial_escape = _split_partial_escape(
            self._partial_escape + chunk.decode("utf-8", errors="replace")
        )
        self._buffer += clean(text)
        if len(self._buffer) > _MAX_BUFFER_CHARS:
            self._buffer = self._buffer[-_MAX_BUFFER_CHARS:]
        return True

    def _expect(
        self,
        patterns: list[re.Pattern[str]],
        timeout: float,
        quiet_retry: str | None = None,
        quiet_after: float = _QUIET_RETRY_SECONDS,
    ) -> tuple[int, str]:
        """
        Waits until one of the patterns matches the buffer. Returns (index, text up to
        and including the match) and keeps the rest. With `quiet_retry`, that text is
        typed whenever the console stays silent for a few seconds (waking it up).
        :raises AgentTimeout: nothing matched in time
        """
        deadline = time.monotonic() + timeout
        last_input = time.monotonic()
        while True:
            for index, pattern in enumerate(patterns):
                match = pattern.search(self._buffer)
                if match:
                    taken, self._buffer = (
                        self._buffer[: match.end()],
                        self._buffer[match.end() :],
                    )
                    return index, taken
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                tail = self._buffer[-80:].replace("\n", "\\n")
                if not self._buffer.strip():
                    raise AgentTimeout(
                        f"the serial console stayed completely silent for {timeout:.0f}s - "
                        f"the guest probably does not use its serial port (Linux needs a "
                        f"getty on ttyS0; pfSense/OPNsense need their serial console switched "
                        f"on in their own settings)"
                    )
                raise AgentTimeout(
                    f"the console showed nothing expected within {timeout:.0f}s (last text: {tail!r})"
                )
            got = self._read(min(0.5, remaining))
            if (
                not got
                and quiet_retry is not None
                and time.monotonic() - last_input >= quiet_after
            ):
                self._send(quiet_retry)
                last_input = time.monotonic()

    # ---- login ----

    def login(self) -> None:
        """
        Gets the console to a usable prompt: answers login, password and menu
        screens, whatever state it was left in. Safe to call on an already
        logged-in console.
        :raises LoginFailed: the credentials were rejected
        :raises AgentTimeout: the console never showed a prompt
        """
        profile = self.profile
        patterns = [profile.shell_prompt, profile.login_prompt, profile.password_prompt]
        menu_index = None
        if profile.menu_prompt is not None:
            patterns.append(profile.menu_prompt)
            menu_index = len(patterns) - 1

        # No Enter up front: a console that is already showing a prompt answers it
        # with a second prompt, and that stale text confuses the dialogue. Only a
        # console that stays silent is woken up.
        deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
        attempts = 0
        quiet_after = _FIRST_QUIET_SECONDS
        while time.monotonic() < deadline:
            index, text = self._expect(
                patterns,
                deadline - time.monotonic(),
                quiet_retry="\r",
                quiet_after=quiet_after,
            )
            quiet_after = _QUIET_RETRY_SECONDS
            if index == 0:
                if profile.menu_prompt is not None and profile.menu_prompt.search(text):
                    continue
                self.logged_in = True
                self._prepare_shell()
                return
            if index == 1:
                attempts += 1
                if attempts > 3:
                    raise LoginFailed(
                        "the console keeps asking for a login - wrong user name or password"
                    )
                self._buffer = ""
                self._send(self.username + profile.login_suffix + "\r")
            elif index == 2:
                self._buffer = ""
                self._send(self.password + "\r")
                self.sleep(0.5)
                self._read(1.0)
                if profile.login_failed.search(self._buffer):
                    raise LoginFailed("the console rejected the user name or password")
            elif index == menu_index:
                self._send(profile.menu_shell_choice + "\r")
        raise AgentTimeout("could not reach a command prompt on the console")

    def _prepare_shell(self) -> None:
        for line in self.profile.shell_setup:
            self._send(line + "\r")
            self.sleep(0.3)
        self._buffer = ""
        # Flush whatever the setup produced (prompts) and make sure we are ready.
        if self.profile.style == "marker":
            self.run("true", timeout=15)
        else:
            # A prompt console redraws its prompt a moment after the login; left in the
            # buffer it would pass for the end of the first command and swallow its output.
            self._drain(quiet=_POST_LOGIN_QUIET_SECONDS)

    # ---- restart ----

    def reboot(self, timeout: float) -> None:
        """
        Restarts the device from its prompt and logs in again once the console shows a login.
        :raises AgentError: the profile has no restart command, or the console did not come back
        """
        profile = self.profile
        if profile.reboot_command is None:
            raise AgentError(
                f"the '{profile.name}' console profile cannot restart the device itself"
            )
        if not self.logged_in:
            raise AgentError("not logged in")
        self._buffer = ""
        self._send(profile.reboot_command + "\r")
        if profile.reboot_confirm is not None:
            self.sleep(1.0)
            self._send(profile.reboot_confirm + "\r")
        self.wait_for_reboot(timeout)

    def wait_for_reboot(self, timeout: float) -> None:
        """
        Waits for the login prompt that only appears once the device came up again, then logs in.
        :raises AgentError: no login prompt within `timeout` seconds
        """
        self.logged_in = False
        self._buffer = ""
        try:
            self._expect([self.profile.login_prompt], timeout)
        except AgentTimeout as exc:
            raise AgentTimeout(
                f"the device did not show a login again within {timeout:.0f}s ({exc})"
            ) from exc
        self.login()
        self._drain(quiet=_POST_REBOOT_QUIET_SECONDS)

    # ---- commands ----

    def run(
        self, command: str, timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS
    ) -> tuple[int | None, str]:
        """
        Runs one command and returns (exit_code, output). exit_code is 0/1 for prompt
        profiles (1 = an error text appeared), the shell's real status for marker
        profiles, and None if the command did not finish in time.
        """
        if not self.logged_in:
            raise AgentError("not logged in")
        if self.profile.style == "marker":
            return self._run_marker(command, timeout)
        return self._run_prompt(command, timeout)

    def _run_marker(self, command: str, timeout: float) -> tuple[int | None, str]:
        token = f"TOPOEND{self._nonce}{uuid.uuid4().hex[:6]}"
        marker = re.compile(rf"{token}(\d+)TOPOEND")
        self._buffer = ""
        # The marker's digits only exist after the shell expanded $?, so the typed
        # command line itself (if the console echoes it) can never match.
        self._send(f"{command}\recho {token}$?TOPOEND\r")
        try:
            _, text = self._expect([marker], timeout)
        except AgentTimeout:
            return None, self._tidy(self._buffer, command)
        status = int(marker.search(text).group(1))
        return status, self._tidy(marker.sub("", text), command)

    def _run_prompt(self, command: str, timeout: float) -> tuple[int | None, str]:
        self._buffer = ""
        self._send(command + "\r")
        try:
            _, text = self._expect([self.profile.shell_prompt], timeout)
        except AgentTimeout:
            return None, self._tidy(self._buffer, command)
        self._drain()
        output = self._tidy(text, command)
        lowered = output.lower()
        failed = any(marker in lowered for marker in self.profile.error_markers)
        return (1 if failed else 0), output

    def _drain(self, quiet: float = 0.08) -> None:
        """
        Swallows what the console still prints after a command's prompt (RouterOS redraws the
        prompt, sometimes with a changed name) so it cannot leak into the next command's output.
        """
        while self._read(quiet):
            pass
        self._buffer = ""

    def _tidy(self, text: str, command: str) -> str:
        lines = text.split("\n")
        # Drop the echo of our own command line and trailing prompt noise.
        first = command.strip().split("\n")[0].strip()
        while lines and (
            not lines[0].strip() or (first and lines[0].strip().endswith(first[-40:]))
        ):
            lines.pop(0)
        while lines and (
            not lines[-1].strip() or self.profile.shell_prompt.search(lines[-1])
        ):
            lines.pop()
        return "\n".join(line.rstrip() for line in lines)[:_MAX_OUTPUT_CHARS]


def _run_directive(
    agent: SerialAgent, directive: push_directives.Directive
) -> tuple[int, str]:
    """@reboot / @wait-reboot on the console: (0, text) once the device is back, (1, reason) if not."""
    started = time.monotonic()
    try:
        if directive.kind == "reboot":
            agent.reboot(directive.timeout)
        else:
            agent.wait_for_reboot(directive.timeout)
    except AgentError as exc:
        return 1, str(exc)
    return 0, f"restarted, back after {time.monotonic() - started:.0f}s"


def run_commands(
    agent: SerialAgent,
    commands: list[str],
    on_progress: Callable[[int, int], None] | None = None,
    results: list[tuple[str, int | None, str]] | None = None,
) -> list[tuple[str, int | None, str]]:
    """
    Runs the commands in order on a logged-in agent and stops at the first failing
    one (a non-zero status, or no completion in time). Returns one
    (command, exit_code, output) per command that was executed. Pass your own `results`
    list to keep what already ran when the connection breaks half way.
    """
    results = results if results is not None else []
    total = len(commands)
    for number, command in enumerate(commands, 1):
        directive = push_directives.parse(command)
        if directive is None:
            code, output = agent.run(command)
        else:
            code, output = _run_directive(agent, directive)
        results.append((command, code, output))
        if on_progress is not None:
            on_progress(number, total)
        if code != 0:
            break
    logger.debug(f"Serial agent ran {len(results)} of {total} command(s)")
    return results
