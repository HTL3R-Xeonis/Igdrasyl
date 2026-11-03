"""
A fresh machine has no active host (no settings file, no activated host profile). The built-in lab address of
TopologyBuilder's defaults is not a host anybody configured: starting must neither warn about it nor try to recover a GNS3 VM
on it.
"""

from unittest.mock import MagicMock, patch

import app.main as main_module


def _refresh(app_state, active_host: str | None, monkeypatch):
    if active_host is None:
        monkeypatch.delenv(main_module._SETTINGS_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(main_module._SETTINGS_ENV_VAR, active_host)
    app_state.esxi_connection = MagicMock(name="esxi")
    app_state.esxi_connection.get_vm_ip_address.return_value = None
    with (
        patch("app.main.load_graph", return_value=app_state.graph),
        patch("app.main.gns3_vm_deploy_jobs.auto_deploy_if_missing") as auto_deploy,
    ):
        main_module._refresh_startup_state(main_module.app)
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
    monkeypatch.delenv(main_module._SETTINGS_ENV_VAR, raising=False)
    with patch("app.main.load_graph", side_effect=ValueError("No project selected.")):
        main_module._refresh_startup_state(main_module.app)
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
