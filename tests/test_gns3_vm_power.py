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


def test_status_offers_deploy_for_a_missing_vm_only_when_a_template_is_selected(
    app_state,
):
    from unittest.mock import patch

    esxi = _esxi(app_state, "poweredOff")
    esxi.get_vm.return_value = None
    with patch("app.gns3_vm_deploy_jobs.is_configured", return_value=True):
        result = gns3_vm_power.status(_app())
    assert result == {
        "name": Settings.ESXI.GNS3_VM_NAME,
        "power_state": None,
        "action": "deploy",
    }
    with patch("app.gns3_vm_deploy_jobs.is_configured", return_value=False):
        assert gns3_vm_power.status(_app())["action"] is None


def test_status_is_none_when_the_state_is_unreadable(app_state):
    esxi = _esxi(app_state, "poweredOff")
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


def test_template_health_carries_the_power_state_only_when_unhealthy(client, app_state):
    from unittest.mock import patch

    _esxi(app_state, "poweredOff")
    app_state.gns3_vm_ip = None
    broken = {"ok": False, "problems": ["The GNS3 template service lists 0 templates."]}
    with patch("app.main.template_health.check", return_value=broken):
        body = client.get("/templates/health").json()
    assert body["gns3_vm"]["action"] == "start"

    healthy = {"ok": True, "problems": []}
    with patch("app.main.template_health.check", return_value=healthy):
        assert "gns3_vm" not in client.get("/templates/health").json()


def test_the_template_banner_offers_the_same_button():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "app" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert 'id="template-gns3vm-btn"' in html
    assert "startGns3Vm(templateGns3Vm" in js
    assert "health.gns3_vm" in js


def test_template_refresh_asks_both_services_and_forgets_the_cached_health(client):
    from unittest.mock import patch

    response = MagicMock(status_code=202)
    with (
        patch("app.template_health.requests.post", return_value=response) as post,
        patch("app.template_health.invalidate") as invalidate,
    ):
        body = client.post("/templates/refresh").json()
    assert body == {"requested": True, "esxi": True, "gns3": True}
    urls = [call.args[0] for call in post.call_args_list]
    assert len(urls) == 2 and all(url.endswith("/api/refresh") for url in urls)
    invalidate.assert_called_once()


def test_template_refresh_tells_an_older_service_from_an_unreachable_one(client):
    from unittest.mock import patch

    import requests

    with patch(
        "app.template_health.requests.post", return_value=MagicMock(status_code=404)
    ):
        body = client.post("/templates/refresh").json()
    assert body == {"requested": False, "esxi": False, "gns3": False}

    answers = [MagicMock(status_code=202), requests.ConnectionError("down")]
    with patch("app.template_health.requests.post", side_effect=answers):
        body = client.post("/templates/refresh").json()
    assert body == {"requested": True, "esxi": True, "gns3": None}


def test_template_health_says_whether_asking_for_a_refresh_can_help(monkeypatch):
    from app import template_health

    def run(esxi, gns3):
        answers = iter([esxi, gns3])

        def fake_count(label, url, problems):
            count = next(answers)
            if not count:
                problems.append(f"{label} problem")
            return count

        monkeypatch.setattr(Settings.API, "LITERAL_API_VALUES", False)
        monkeypatch.setattr(template_health, "_template_count", fake_count)
        monkeypatch.setattr(template_health, "_gns3_server_problems", lambda *a: None)
        return template_health._run_check()

    assert run(3, 0)["refreshable"] is True  # a service answered but lists nothing
    assert run(None, None)["refreshable"] is False  # nothing answered, nothing to ask
    healthy = run(3, 4)
    assert healthy["ok"] is True and healthy["refreshable"] is False


def test_both_banners_say_what_the_start_is_doing_instead_of_the_old_problem_text():
    from pathlib import Path

    js = (
        Path(__file__).resolve().parent.parent / "app" / "static" / "app.js"
    ).read_text(encoding="utf-8")
    assert "setGns3VmProgress(" in js
    assert "text && gns3VmProgress ? gns3VmProgress : text" in js
    assert "gns3VmProgress || `Template service problem" in js
    assert "gns3VmProgress = null;" in js
