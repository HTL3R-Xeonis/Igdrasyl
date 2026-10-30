"""@reboot / @wait-reboot lines in a device's command list."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import app.config_push as cp
from app import push_directives as pd


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("@reboot", pd.Directive("reboot", 900)),
        ("  @reboot 120 ", pd.Directive("reboot", 120)),
        ("@WAIT-REBOOT", pd.Directive("wait-reboot", 900)),
        ("@wait-reboot 30", pd.Directive("wait-reboot", 30)),
        ("@echo off", None),
        ("@reboot now", None),
        ("reboot", None),
        ("/system reboot", None),
    ],
)
def test_only_the_two_directives_are_recognised(line, expected):
    assert pd.parse(line) == expected


class _Guest:
    """toolsRunningStatus / guestOperationsReady as they change over successive polls."""

    def __init__(self, states):
        self._states = list(states)

    def _state(self):
        return self._states[0] if len(self._states) == 1 else self._states.pop(0)

    @property
    def toolsRunningStatus(self):
        return "guestToolsRunning" if self._state() else "guestToolsNotRunning"

    @property
    def guestOperationsReady(self):
        return self._states[0] if self._states else False


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _vm(*states):
    vm = MagicMock()
    vm.guest = _Guest(states)
    return vm


def test_wait_returns_once_the_guest_went_down_and_is_back():
    clock = _Clock()
    vm = _vm(True, True, False, False, False, True)
    text = pd.wait_for_guest_reboot(vm, 600, sleep=clock.sleep, clock=clock)
    assert text.startswith("restarted, back after")


def test_wait_fails_when_the_guest_never_goes_down():
    clock = _Clock()
    with pytest.raises(pd.RebootError, match="did not restart"):
        pd.wait_for_guest_reboot(_vm(True), 600, sleep=clock.sleep, clock=clock)


def test_wait_fails_when_the_guest_does_not_come_back():
    clock = _Clock()
    with pytest.raises(pd.RebootError, match="not back within 60s"):
        pd.wait_for_guest_reboot(_vm(True, False), 60, sleep=clock.sleep, clock=clock)


def _push(commands, vm):
    esxi = MagicMock()
    esxi.get_vm.return_value = vm
    process_manager = esxi.content.guestOperationsManager.processManager
    process_manager.StartProgramInGuest.return_value = 1
    process_manager.ListProcessesInGuest.return_value = [
        MagicMock(endTime="t", exitCode=0)
    ]
    node = MagicMock(image="Rocky 9.2")
    node.name = "VM1"
    with patch.object(cp, "guest_credentials_for_image", return_value=("u", "p")):
        return cp._push_via_guest_ops(node, commands, esxi), process_manager


def test_a_reboot_in_the_middle_of_a_guest_ops_push_is_waited_for():
    vm = MagicMock()
    with patch.object(
        pd, "wait_for_guest_reboot", return_value="restarted, back after 7s"
    ) as wait:
        result, process_manager = _push(["first", "@reboot 120", "second"], vm)
    assert result.ok and [c.command for c in result.commands] == [
        "first",
        "@reboot 120",
        "second",
    ]
    vm.RebootGuest.assert_called_once()
    assert wait.call_args.args[1] == 120
    assert (
        process_manager.StartProgramInGuest.call_count == 2
    )  # the directive is not sent


def test_wait_reboot_does_not_restart_the_guest_itself():
    vm = MagicMock()
    with patch.object(pd, "wait_for_guest_reboot", return_value="ok"):
        result, _ = _push(["promote", "@wait-reboot", "after"], vm)
    assert result.ok
    vm.RebootGuest.assert_not_called()


def test_a_failed_reboot_stops_the_rest_of_the_list():
    vm = MagicMock()
    vm.RebootGuest.side_effect = RuntimeError("tools are gone")
    result, process_manager = _push(["first", "@reboot", "never"], vm)
    assert not result.ok and result.executed == 2
    assert "could not restart the guest" in result.commands[1].output
    assert process_manager.StartProgramInGuest.call_count == 1
    assert result.not_ready is False


def test_a_gns3_device_rejects_the_directives_before_connecting(graph):
    result = cp._push_via_netmiko(
        graph.nodes["R1"], ["show version", "@reboot"], "127.0.0.1", 1
    )
    assert not result.ok and "not supported on GNS3" in result.error
    assert result.executed == 0
