from unittest.mock import MagicMock

import pytest
from src.settings import Settings

from app import esxi_session, gns3_vm_power


def _app():
    from app.main import app

    return app


def _vm(power_state):
    vm = MagicMock()
    vm.runtime.powerState = power_state
    return vm


def _esxi(app_state, power_state, connected=True):
    esxi = MagicMock()
    esxi.connected = connected
    esxi.get_vm.return_value = _vm(power_state)
    app_state.esxi_connection = esxi
    return esxi


def test_status_offers_start_for_a_powered_off_vm(app_state):
    app_state.gns3_vm_ip = None
    esxi = _esxi(app_state, "poweredOff")
    result = gns3_vm_power.status(_app())
    assert result == {
        "name": Settings.ESXI.GNS3_VM_NAME,
        "power_state": "poweredOff",
        "action": "start",
    }
    esxi.get_vm.assert_called_with(Settings.ESXI.GNS3_VM_NAME)


def test_status_offers_restart_only_for_a_running_vm_without_address(app_state):
    _esxi(app_state, "poweredOn")
    app_state.gns3_vm_ip = None
    assert gns3_vm_power.status(_app())["action"] == "restart"
    app_state.gns3_vm_ip = "10.20.20.227"
    assert gns3_vm_power.status(_app())["action"] is None


def test_status_never_connects_to_esxi(app_state):
    esxi = _esxi(app_state, "poweredOff", connected=False)
    assert gns3_vm_power.status(_app()) is None
    esxi.get_vm.assert_not_called()


def test_status_is_none_when_the_vm_is_missing_or_unreadable(app_state):
    esxi = _esxi(app_state, "poweredOff")
    esxi.get_vm.return_value = None
    assert gns3_vm_power.status(_app()) is None
    esxi.get_vm.side_effect = RuntimeError("session lost")
    assert gns3_vm_power.status(_app()) is None


def test_start_powers_a_stopped_or_suspended_vm_on(app_state):
    for state in ("poweredOff", "suspended"):
        esxi = _esxi(app_state, state)
        assert gns3_vm_power.start(_app()) == "started"
        esxi.power_on_vm.assert_called_once()
        esxi.reset_vm.assert_not_called()


def test_start_resets_a_running_vm_without_address_and_leaves_a_healthy_one(app_state):
    app_state.gns3_vm_ip = None
    esxi = _esxi(app_state, "poweredOn")
    assert gns3_vm_power.start(_app()) == "restarted"
    esxi.reset_vm.assert_called_once()

    app_state.gns3_vm_ip = "10.20.20.227"
    esxi = _esxi(app_state, "poweredOn")
    assert gns3_vm_power.start(_app()) == "running"
    esxi.reset_vm.assert_not_called()
    esxi.power_on_vm.assert_not_called()


def test_start_says_so_when_the_vm_does_not_exist(app_state):
    esxi = _esxi(app_state, "poweredOff")
    esxi.get_vm.return_value = None
    with pytest.raises(ValueError, match="not found"):
        gns3_vm_power.start(_app())


def test_health_carries_the_power_state_only_while_something_is_wrong(
    client, app_state
):
    _esxi(app_state, "poweredOff")
    app_state.gns3_vm_ip = "10.20.20.227"
    app_state.startup_warning = None
    assert client.get("/health").json()["gns3_vm"] is None
    app_state.startup_warning = "Template service problem"
    assert client.get("/health").json()["gns3_vm"]["action"] == "start"
    app_state.startup_warning = None
    app_state.gns3_vm_ip = None
    assert client.get("/health").json()["gns3_vm"]["action"] == "start"


def test_start_endpoint_reports_the_result(client, app_state):
    esxi = _esxi(app_state, "poweredOff")
    resp = client.post("/gns3-vm/start")
    assert resp.status_code == 200
    assert resp.json() == {"result": "started"}
    esxi.power_on_vm.assert_called_once()


def test_start_endpoint_maps_errors(client, app_state):
    esxi = _esxi(app_state, "poweredOff")
    esxi.power_on_vm.side_effect = RuntimeError("insufficient resources")
    resp = client.post("/gns3-vm/start")
    assert resp.status_code == 502
    assert "insufficient resources" in resp.json()["detail"]

    esxi.get_vm.return_value = None
    assert client.post("/gns3-vm/start").status_code == 404

    esxi.get_vm.side_effect = esxi_session.EsxiUnavailable("host down")
    assert client.post("/gns3-vm/start").status_code == 503


def test_the_banner_offers_a_start_button_for_the_gns3_vm():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "app" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert 'id="warning-gns3vm-btn"' in html
    assert 'fetch("/gns3-vm/start", { method: "POST" })' in js
    assert "gns3Vm: designMode ? null : health.gns3_vm" in js
