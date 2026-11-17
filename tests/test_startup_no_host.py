"""
A fresh machine has no active host (no settings file, no activated host profile). The built-in lab address of
TopologyBuilder's defaults is not a host anybody configured: starting must neither warn about it nor try to recover a GNS3 VM
on it.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.settings import Settings

import app.main as main_module
from app import gateway_main, startup
from app.routes.common import SETTINGS_ENV_VAR
from app.startup import refresh_startup_state


def _refresh(app_state, active_host: str | None, monkeypatch):
    if active_host is None:
        monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(SETTINGS_ENV_VAR, active_host)
    app_state.esxi_connection = MagicMock(name="esxi")
    app_state.esxi_connection.get_vm_ip_address.return_value = None
    with (
        patch("app.startup.load_graph", return_value=app_state.graph),
        patch("app.gns3_vm_deploy_jobs.auto_deploy_if_missing") as auto_deploy,
    ):
        refresh_startup_state(main_module.app)
    return auto_deploy


def test_without_an_active_host_nothing_is_looked_up_and_nothing_is_warned_about(
    app_state, monkeypatch
):
    auto_deploy = _refresh(app_state, None, monkeypatch)
    assert app_state.esxi_connection.get_vm_ip_address.call_count == 0
    auto_deploy.assert_not_called()
    assert app_state.gns3_vm_ip is None and app_state.project_id is None
    assert app_state.startup_warning is None


def test_the_topology_problem_is_still_reported_without_an_active_host(
    app_state, monkeypatch
):
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    with patch(
        "app.startup.load_graph", side_effect=ValueError("No project selected.")
    ):
        refresh_startup_state(main_module.app)
    assert (
        app_state.graph is None and "No project selected" in app_state.startup_warning
    )
    assert "ESXi" not in app_state.startup_warning


def test_with_an_active_host_the_gns3_vm_is_looked_for_as_before(
    app_state, monkeypatch
):
    auto_deploy = _refresh(app_state, "lab.yml", monkeypatch)
    app_state.esxi_connection.get_vm_ip_address.assert_called_once()
    auto_deploy.assert_called_once()
    assert "not found or has no IP" in app_state.startup_warning


@pytest.mark.parametrize(
    ("profile", "expected_ip"), [(None, None), ("lab.yml", "192.0.2.10")]
)
def test_health_reports_the_esxi_ip_only_for_an_active_profile(
    client, app_state, monkeypatch, profile, expected_ip
):
    _refresh(app_state, profile, monkeypatch)
    monkeypatch.setattr(Settings.ESXI, "IP", "192.0.2.10")
    assert client.get("/health").json()["esxi_ip"] == expected_ip


@pytest.mark.parametrize("behind_gateway", [False, True])
def test_initial_admin_belongs_to_whichever_app_handles_first_boot(
    tmp_path, monkeypatch, behind_gateway
):
    user_store = tmp_path / "users.json"
    monkeypatch.setenv("TOPOLOGYOPERATOR_USERS_FILE", str(user_store))
    # a managed backend and the gateway refuse to start without all four shared paths (app/shared_state.py)
    monkeypatch.setenv(
        "TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR", str(tmp_path / "profiles")
    )
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    monkeypatch.delenv("TOPOLOGYOPERATOR_GATEWAY_BACKEND", raising=False)
    if behind_gateway:
        monkeypatch.setenv("TOPOLOGYOPERATOR_GATEWAY_BACKEND", "1")
    monkeypatch.setattr(startup, "configure_logging", lambda: None)
    monkeypatch.setattr(startup, "refresh_startup_state", lambda _app: None)
    monkeypatch.setattr(startup.ova_templates, "initialise", lambda _path: None)
    monkeypatch.setattr(startup.project_selection, "initialise", lambda _path: None)
    monkeypatch.setattr(startup.design_store, "load", lambda: None)
    monkeypatch.setattr(startup.startup_retry, "start", lambda *_args: None)
    monkeypatch.setattr(gateway_main.logging_setup, "configure_logging", lambda: None)
    monkeypatch.setattr(
        gateway_main.Settings, "LOG_FILE_PATH", str(tmp_path / "gateway.log")
    )
    with TestClient(FastAPI(lifespan=startup.lifespan)):
        assert user_store.exists() == (not behind_gateway)

    if behind_gateway:
        with TestClient(FastAPI(lifespan=gateway_main._lifespan)):
            assert [record["username"] for record in startup.users.list_users()] == [
                "admin"
            ]
