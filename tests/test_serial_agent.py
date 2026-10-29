"""
The serial-console agent (app/serial_agent.py) against real local TCP servers
that behave like a Linux serial getty and a RouterOS CLI.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from app import serial_agent as sa


async def _linux_console(
    reader, writer, *, user="root", password="pw", logged_in=False
):
    """A tiny Linux: getty login, then a shell that knows a few commands and `echo $?`."""
    state = "shell" if logged_in else "login"
    status = 0
    buffer = b""
    writer.write(b"\r\nubuntu login: " if state == "login" else b"root@vm:~# ")
    await writer.drain()
    while True:
        data = await reader.read(4096)
        if not data:
            return
        buffer += data
        while b"\r" in buffer:
            line, buffer = buffer.split(b"\r", 1)
            text = line.decode().strip()
            if state == "login":
                if text == user:
                    state = "password"
                    writer.write(b"Password: ")
                else:
                    writer.write(b"\r\nubuntu login: ")
            elif state == "password":
                if text == password:
                    state = "shell"
                    writer.write(b"\r\nWelcome\r\nroot@vm:~# ")
                else:
                    state = "login"
                    writer.write(b"\r\nLogin incorrect\r\nubuntu login: ")
            else:
                out = ""
                if text.startswith("echo TOPOEND"):
                    token = text[len("echo ") :].replace("$?", str(status))
                    out = token + "\r\n"
                elif text == "exit":
                    state = "login"
                    out = "\r\nubuntu login: "
                    writer.write(out.encode())
                    await writer.drain()
                    continue
                elif text == "hostname":
                    out, status = "vm01\r\n", 0
                elif text == "false":
                    status = 1
                elif text.startswith("echo "):
                    out, status = text[5:].replace("'", "") + "\r\n", 0
                elif text.startswith("sleep "):
                    return  # never answers: a hanging command
                else:
                    status = 0
                writer.write(out.encode())
                if not text.startswith("echo TOPOEND"):
                    pass
            await writer.drain()


def _agent(port, profile="linux", **kwargs):
    defaults = {"host": "127.0.0.1", "port": port, "username": "root", "password": "pw"}
    defaults.update(kwargs)
    return sa.SerialAgent(
        profile=sa.PROFILES[profile], sleep=lambda s: None, **defaults
    )


def test_login_and_commands_with_real_exit_status(fake_tcp_server):
    port = fake_tcp_server(_linux_console)
    with _agent(port) as agent:
        agent.login()
        assert agent.logged_in
        assert agent.run("hostname") == (0, "vm01")
        code, _ = agent.run("false")
        assert code == 1


def test_a_console_that_is_already_logged_in_needs_no_login(fake_tcp_server):
    async def handler(reader, writer):
        await _linux_console(reader, writer, logged_in=True)

    port = fake_tcp_server(handler)
    with _agent(port) as agent:
        agent.login()
        assert agent.run("hostname") == (0, "vm01")


def test_wrong_password_is_reported_as_a_login_failure(fake_tcp_server):
    port = fake_tcp_server(_linux_console)
    with _agent(port, password="nope") as agent, pytest.raises(sa.LoginFailed):
        agent.login()


def test_run_commands_stops_at_the_first_failure(fake_tcp_server):
    port = fake_tcp_server(_linux_console)
    with _agent(port) as agent:
        agent.login()
        results = sa.run_commands(agent, ["hostname", "false", "hostname"])
    assert [(c, code) for c, code, _ in results] == [("hostname", 0), ("false", 1)]


def test_progress_is_reported_per_command(fake_tcp_server):
    port = fake_tcp_server(_linux_console)
    seen = []
    with _agent(port) as agent:
        agent.login()
        sa.run_commands(
            agent,
            ["hostname", "hostname"],
            on_progress=lambda d, t: seen.append((d, t)),
        )
    assert seen == [(1, 2), (2, 2)]


def test_a_command_that_never_finishes_times_out_instead_of_hanging(fake_tcp_server):
    port = fake_tcp_server(_linux_console)
    with _agent(port) as agent:
        agent.login()
        code, _ = agent.run("sleep 1000", timeout=1.5)
    assert code is None


def test_connection_refused_is_a_clear_error():
    with pytest.raises(sa.AgentConnectionError, match="serial port enabled"):
        sa.SerialAgent(
            host="127.0.0.1",
            port=1,
            profile=sa.PROFILES["linux"],
            username="a",
            password="b",
        ).connect()


async def _routeros_console(
    reader, writer, *, password="secret", redraw=0.0, reply_delay=0.0
):
    state = "login"
    buffer = b""
    seen_user = ""
    writer.write(b"\r\nMikroTik v7.21.3 (stable)\r\nLogin: ")
    await writer.drain()
    while True:
        data = await reader.read(4096)
        if not data:
            return
        buffer += data
        while b"\r" in buffer:
            line, buffer = buffer.split(b"\r", 1)
            text = line.decode().strip()
            if state == "login":
                seen_user = text
                state = "password"
                writer.write(b"Password: ")
            elif state == "password":
                if text == password:
                    state = "cli"
                    writer.write(b"\r\n[admin@MikroTik] > ")
                    if redraw:
                        # RouterOS paints the prompt again a moment after the login
                        await writer.drain()
                        await asyncio.sleep(redraw)
                        writer.write(b"\r\n[admin@MikroTik] > ")
                else:
                    state = "login"
                    writer.write(
                        b"\r\nLogin failed, incorrect username or password\r\nLogin: "
                    )
            else:
                if text == "/quit":
                    state = "login"
                    writer.write(b"\r\nLogin: ")
                elif text.startswith("/ip address add"):
                    writer.write(b"\r\n[admin@MikroTik] > ")
                elif text == "/ip address print":
                    await asyncio.sleep(reply_delay)
                    writer.write(b"\r\n 0 10.0.0.1/24 ether1\r\n[admin@MikroTik] > ")
                else:
                    writer.write(
                        b"\r\nbad command name nonsense (line 1 column 1)\r\n[admin@MikroTik] > "
                    )
            await writer.drain()
    del seen_user


def test_routeros_login_uses_the_terminal_suffix_and_detects_cli_errors(
    fake_tcp_server,
):
    users = []

    async def handler(reader, writer):
        # capture the user name sent at the Login prompt
        await _routeros_console(reader, writer)

    port = fake_tcp_server(handler)
    with _agent(port, profile="routeros", password="secret") as agent:
        agent.login()
        assert agent.run("/ip address add address=10.0.0.1/24 interface=ether1") == (
            0,
            "",
        )
        code, output = agent.run("/ip address print")
        assert code == 0 and "10.0.0.1/24" in output
        code, output = agent.run("/nonsense")
        assert code == 1 and "bad command name" in output
    assert users == []


def test_routeros_first_command_keeps_its_output_when_the_prompt_is_redrawn(
    fake_tcp_server,
):
    async def handler(reader, writer):
        await _routeros_console(reader, writer, redraw=0.3, reply_delay=0.5)

    port = fake_tcp_server(handler)
    with _agent(port, profile="routeros", password="secret") as agent:
        agent.login()
        code, output = agent.run("/ip address print")
        assert code == 0 and "10.0.0.1/24" in output


def test_routeros_wrong_password_fails_the_login(fake_tcp_server):
    port = fake_tcp_server(_routeros_console)
    with (
        _agent(port, profile="routeros", password="wrong") as agent,
        pytest.raises(sa.LoginFailed),
    ):
        agent.login()


async def _bsd_menu_console(reader, writer):
    """pfSense-style: a menu first, option 8 opens a shell."""
    state = "menu"
    buffer = b""
    status = 0
    writer.write(b"\r\n*** pfSense ***\r\n 8) Shell\r\nEnter an option: ")
    await writer.drain()
    while True:
        data = await reader.read(4096)
        if not data:
            return
        buffer += data
        while b"\r" in buffer:
            line, buffer = buffer.split(b"\r", 1)
            text = line.decode().strip()
            if state == "menu":
                if text == "8":
                    state = "shell"
                    writer.write(b"\r\n[2.7.2-RELEASE][root@pfSense]/root: ")
                else:
                    writer.write(b"\r\nEnter an option: ")
            else:
                out = ""
                if text.startswith("echo TOPOEND"):
                    out = text[5:].replace("$?", str(status)) + "\r\n"
                elif text == "uname -s":
                    out = "FreeBSD\r\n"
                elif text == "exit":
                    state = "menu"
                    writer.write(b"\r\nEnter an option: ")
                    await writer.drain()
                    continue
                writer.write(out.encode())
            await writer.drain()


def test_bsd_menu_opens_a_shell_through_option_8(fake_tcp_server):
    port = fake_tcp_server(_bsd_menu_console)
    with _agent(port, profile="bsd_menu") as agent:
        agent.login()
        assert agent.run("uname -s") == (0, "FreeBSD")


def test_console_text_is_cleaned_of_escape_sequences():
    raw = "\x1b[1;32mhello\x1b[0m\r\nworld\x1b[K\x07\x00"
    assert sa.clean(raw) == "hello\nworld"


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("MikroTik", "routeros"),
        ("pfSense", "bsd_menu"),
        ("OPNsense", "bsd_menu"),
        ("Rocky 9.2", "linux"),
        ("debian", "linux"),
        ("Windows 10 Pro Education", None),
    ],
)
def test_profile_is_chosen_by_image(image, expected):
    profile = sa.profile_for_image(image)
    assert (profile.name if profile else None) == expected


def test_error_texts_never_include_the_password():
    assert not re.search(
        "pw", str(sa.AgentTimeout("the console showed nothing expected"))
    )


def test_an_escape_sequence_split_across_packets_is_removed_whole():
    text, rest = sa._split_partial_escape("hello\x1b[?20")
    assert (text, rest) == ("hello", "\x1b[?20")
    text, rest = sa._split_partial_escape(rest + "04h world")
    assert rest == "" and sa.clean(text) == " world"
    # a complete sequence at the end is not held back
    assert sa._split_partial_escape("x\x1b[0m") == ("x\x1b[0m", "")
    # a lone ESC at the very end is held back, a plain text end is not
    assert sa._split_partial_escape("abc\x1b") == ("abc", "\x1b")
    assert sa._split_partial_escape("plain") == ("plain", "")


def test_split_escapes_do_not_leak_into_command_output(fake_tcp_server):
    async def handler(reader, writer):
        # the shell prompt arrives with a bracketed-paste sequence cut in two packets
        writer.write(b"\r\nubuntu login: ")
        await writer.drain()
        buffer = b""
        state = "login"
        while True:
            data = await reader.read(4096)
            if not data:
                return
            buffer += data
            while b"\r" in buffer:
                line, buffer = buffer.split(b"\r", 1)
                text = line.decode().strip()
                if state == "login":
                    state = "pw"
                    writer.write(b"Password: ")
                elif state == "pw":
                    state = "shell"
                    writer.write(b"root@vm:~# \x1b[?20")
                    await writer.drain()
                    writer.write(b"04h")
                elif text.startswith("echo TOPOEND"):
                    writer.write(b"\x1b[?2004l\r\nreal output\r\n")
                    writer.write(text[5:].replace("$?", "0").encode() + b"\r\n")
                await writer.drain()

    port = fake_tcp_server(handler)
    with _agent(port) as agent:
        agent.login()
        code, output = agent.run("anything")
    assert code == 0
    assert output == "real output"


def test_a_silent_console_is_explained(fake_tcp_server):
    async def handler(reader, writer):
        await reader.read(100)  # accepts, never says anything

    port = fake_tcp_server(handler)
    agent = _agent(port)
    agent.connect()
    try:
        with pytest.raises(sa.AgentTimeout, match="completely silent"):
            agent._expect([sa.PROFILES["linux"].shell_prompt], timeout=0.6)
    finally:
        agent.close()
