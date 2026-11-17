"""A gateway or managed backend refuses to start without absolute shared-state paths; standalone is unchanged."""

import asyncio

import pytest
from loguru import logger
from src.settings import Settings

from app import gateway_main, shared_state, startup
from app.application import app as backend_app

ABSOLUTE = {
    "TOPOLOGYOPERATOR_USERS_FILE": "/srv/x/users.json",
    "TOPOLOGYOPERATOR_SESSION_SECRET_FILE": "/srv/x/session_secret.key",
    "TOPOLOGYOPERATOR_AI_CONFIG_FILE": "/srv/x/ai_config.json",
    "TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR": "/srv/x/settings_profiles",
}


@pytest.fixture
def absolute_paths(monkeypatch):
    for name, value in ABSOLUTE.items():
        monkeypatch.setenv(name, value)


def test_all_four_absolute_is_fine(absolute_paths):
    shared_state.require_shared_state_paths()


@pytest.mark.parametrize("variable", shared_state.SHARED_STATE_VARIABLES)
def test_a_missing_variable_stops_with_the_exact_text(
    absolute_paths, monkeypatch, variable
):
    monkeypatch.delenv(variable)
    with pytest.raises(RuntimeError) as raised:
        shared_state.require_shared_state_paths()
    assert str(raised.value) == (
        f"{variable} is not set to an absolute path - every gateway and backend unit "
        "must share the same file (see deploy/README.md)."
    )


@pytest.mark.parametrize("variable", shared_state.SHARED_STATE_VARIABLES)
def test_a_relative_path_stops_too(absolute_paths, monkeypatch, variable):
    monkeypatch.setenv(variable, "relative/file")
    with pytest.raises(RuntimeError, match=variable):
        shared_state.require_shared_state_paths()


def test_every_offender_is_named_and_logged(monkeypatch):
    for name in ABSOLUTE:
        monkeypatch.delenv(name, raising=False)
    messages = []
    sink = logger.add(lambda message: messages.append(str(message)), level="ERROR")
    try:
        with pytest.raises(RuntimeError) as raised:
            shared_state.require_shared_state_paths()
    finally:
        logger.remove(sink)
    for name in ABSOLUTE:
        assert name in str(raised.value)
        assert any(name in line for line in messages)


async def _enter(lifespan):
    async with lifespan:
        pass


def test_a_managed_backend_without_the_files_does_not_start(monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_GATEWAY_BACKEND", "1")
    monkeypatch.setenv("TOPOLOGYOPERATOR_USERS_FILE", "users.json")
    with pytest.raises(RuntimeError, match="TOPOLOGYOPERATOR_USERS_FILE"):
        asyncio.run(_enter(startup.lifespan(backend_app)))


def test_the_gateway_without_the_files_does_not_start(monkeypatch):
    for name in ABSOLUTE:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="TOPOLOGYOPERATOR_SESSION_SECRET_FILE"):
        asyncio.run(_enter(gateway_main._lifespan(gateway_main.app)))


def test_the_gateway_starts_with_absolute_paths(absolute_paths, monkeypatch):
    monkeypatch.setattr(gateway_main.logging_setup, "configure_logging", lambda: None)
    monkeypatch.setattr(gateway_main.users, "ensure_bootstrap_admin", lambda: None)
    asyncio.run(_enter(gateway_main._lifespan(gateway_main.app)))


def test_the_check_runs_only_for_a_managed_backend(monkeypatch):
    called = []
    monkeypatch.setattr(
        startup.shared_state, "require_shared_state_paths", lambda: called.append(1)
    )
    monkeypatch.delenv("TOPOLOGYOPERATOR_GATEWAY_BACKEND", raising=False)
    startup.check_shared_state_if_managed()
    assert called == []  # standalone: relative defaults stay fine
    monkeypatch.setenv("TOPOLOGYOPERATOR_GATEWAY_BACKEND", "1")
    startup.check_shared_state_if_managed()
    assert called == [1]


def test_literal_api_values_warns_only_when_set(monkeypatch):
    messages = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        monkeypatch.setattr(Settings.API, "LITERAL_API_VALUES", False)
        startup.warn_if_literal_api_values()
        assert messages == []
        monkeypatch.setattr(Settings.API, "LITERAL_API_VALUES", True)
        startup.warn_if_literal_api_values()
    finally:
        logger.remove(sink)
    assert len(messages) == 1
    assert (
        "LITERAL_API_VALUES is set: the template lists are hardcoded, "
        "the template services are NOT asked." in messages[0]
    )
