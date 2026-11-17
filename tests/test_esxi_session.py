"""The ESXi session is opened on first use, so a host that is down or not set up yet does not stop the app starting."""

import copy
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.startup as startup_module
from app import esxi_session
from app.esxi_session import EsxiUnavailable, LazyESXiConnection


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _lazy(connect, clock=None, wait=None):
    return LazyESXiConnection(
        connect, host="10.9.9.9", clock=clock or _Clock(), connect_wait_seconds=wait
    )


def test_nothing_is_connected_until_it_is_used():
    connect = MagicMock()
    lazy = _lazy(connect)
    assert not lazy.connected
    connect.assert_not_called()


def test_the_first_use_connects_once_and_hands_through_the_real_attributes():
    real = MagicMock()
    real.get_vm.return_value = "the vm"
    connect = MagicMock(return_value=real)
    lazy = _lazy(connect)

    assert lazy.get_vm("VM1") == "the vm"
    assert lazy.get_vm("VM2") == "the vm"
    assert lazy.content is real.content
    connect.assert_called_once()
    assert lazy.connected


def test_dunder_probes_do_not_open_a_session():
    connect = MagicMock()
    lazy = _lazy(connect)
    assert not hasattr(lazy, "__iter__")
    copy.copy(lazy)
    connect.assert_not_called()


def test_a_failed_connect_is_an_esxi_unavailable_with_our_own_wording():
    lazy = _lazy(MagicMock(side_effect=OSError("[Errno 113] secret internal detail")))
    with pytest.raises(EsxiUnavailable) as exc_info:
        lazy.get_vm("VM1")
    assert isinstance(
        exc_info.value, RuntimeError
    )  # the existing "except RuntimeError" paths keep working
    assert "10.9.9.9" in str(exc_info.value)
    assert "secret internal detail" not in str(exc_info.value)


def test_after_a_failure_it_waits_before_trying_again_then_recovers():
    clock = _Clock()
    real = MagicMock()
    connect = MagicMock(side_effect=[OSError("down"), real])
    lazy = _lazy(connect, clock)

    with pytest.raises(EsxiUnavailable):
        lazy.get_vm("VM1")
    clock.now += LazyESXiConnection.RETRY_AFTER_SECONDS - 1
    with pytest.raises(EsxiUnavailable):
        lazy.get_vm("VM1")
    assert connect.call_count == 1  # no second connect attempt inside the wait

    clock.now += 2
    lazy.get_vm("VM1")
    assert connect.call_count == 2
    assert lazy.connected


def test_a_connected_session_is_not_replaced_after_a_later_slow_moment():
    clock = _Clock()
    connect = MagicMock(return_value=MagicMock())
    lazy = _lazy(connect, clock)
    lazy.get_vm("VM1")
    clock.now += 3600
    lazy.get_vm("VM1")
    connect.assert_called_once()


# ---------- the app ----------


def test_an_unreachable_esxi_gives_a_clear_message_instead_of_a_500(client, app_state):
    app_state.esxi_connection = _lazy(MagicMock(side_effect=OSError("down")))
    read = client.get("/nodes")
    assert read.status_code == 503  # the new handler
    assert "10.9.9.9" in read.json()["detail"]
    write = client.post("/nodes/VM1/start")
    assert (
        write.status_code == 502
    )  # an endpoint's own existing mapping of RuntimeError
    assert "10.9.9.9" in write.json()["detail"]


def test_the_handler_answers_503_with_the_message_for_api_clients_and_browsers(
    client, app_state
):
    import asyncio

    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/x",
        "headers": [(b"accept", b"application/json")],
        "query_string": b"",
    }
    response = asyncio.run(
        main_module.esxi_unavailable(Request(scope), EsxiUnavailable("host down"))
    )
    assert response.status_code == 503
    assert b"host down" in response.body

    scope["headers"] = [(b"accept", b"text/html")]
    page = asyncio.run(
        main_module.esxi_unavailable(Request(scope), EsxiUnavailable("host down"))
    )
    assert page.status_code == 503 and page.media_type == "text/html"


@pytest.mark.parametrize("password", [None, "fixture-password"])
def test_the_app_starts_when_esxi_is_unavailable(monkeypatch, password):
    monkeypatch.delenv(startup_module.SETTINGS_ENV_VAR, raising=False)
    monkeypatch.setattr(startup_module.Settings.ESXI, "PASSWORD", password)
    app = main_module.app
    saved = dict(app.state._state)
    esxi = MagicMock(side_effect=OSError("host down"))
    try:
        with (
            patch.object(startup_module, "ESXiConnection", esxi),
            patch.object(startup_module, "refresh_startup_state"),
            patch.object(startup_module, "configure_logging"),
            patch.object(startup_module.startup_retry, "start"),
            patch.object(startup_module.ova_templates, "initialise"),
            patch.object(startup_module.project_selection, "initialise"),
            TestClient(app) as started,
        ):
            esxi.assert_not_called()  # startup did not touch ESXi
            assert started.get("/health").status_code == 200
            assert isinstance(
                app.state.esxi_connection, esxi_session.LazyESXiConnection
            )
            with pytest.raises(EsxiUnavailable) as exc_info:
                app.state.esxi_connection.get_vm("VM1")
            if password is None:
                assert "Host settings > Credentials" in str(exc_info.value)
                esxi.assert_not_called()
            else:
                esxi.assert_called_once()
    finally:
        app.state._state.clear()
        app.state._state.update(saved)


# ---------- a host that does not answer at all ----------


def test_a_host_that_does_not_answer_makes_a_call_wait_only_a_little_and_the_connect_goes_on():
    release = threading.Event()
    real = MagicMock()

    def slow_connect():
        release.wait(10)
        return real

    lazy = _lazy(slow_connect, wait=0.2)
    started = time.monotonic()
    with pytest.raises(EsxiUnavailable, match="did not answer within"):
        lazy.get_vm("VM1")
    assert time.monotonic() - started < 2  # not the 10 s the connect would take
    assert not lazy.connected
    release.set()  # the host answers after all
    for _ in range(50):
        try:
            lazy.get_vm("VM1")
            break
        except EsxiUnavailable:
            time.sleep(0.05)
    assert lazy.connected
    real.get_vm.assert_called()


def test_while_a_slow_connect_is_pending_no_second_one_is_started():
    release = threading.Event()
    connect = MagicMock(side_effect=lambda: release.wait(10))
    lazy = _lazy(connect, wait=0.1)
    for _ in range(3):
        with pytest.raises(EsxiUnavailable):
            lazy.get_vm("VM1")
    assert connect.call_count == 1
    release.set()


def test_a_connect_that_fails_late_is_recorded_as_a_failure():
    release = threading.Event()

    def failing_connect():
        release.wait(10)
        raise OSError("timed out")

    clock = _Clock()
    lazy = _lazy(failing_connect, clock, wait=0.1)
    with pytest.raises(EsxiUnavailable):
        lazy.get_vm("VM1")
    release.set()
    time.sleep(0.2)
    with pytest.raises(EsxiUnavailable, match="can't be reached"):
        lazy.get_vm("VM1")
    assert not lazy.connected
