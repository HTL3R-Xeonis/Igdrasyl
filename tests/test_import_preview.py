import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import yaml
from frontend_js import read_app_js
from src.settings import Settings

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def _bundle(*names):
    topology = {"nodes": [{"names": list(names), "role": "VM", "image": "debian"}]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("topology.yaml", yaml.safe_dump(topology))
    return buf.getvalue()


def _preview(client, *names):
    return client.post(
        "/project/import/preview",
        files={"file": ("p.topoproj", _bundle(*names), "application/zip")},
    ).json()


def test_a_full_deploy_host_names_the_devices_the_import_would_delete(
    client, app_state, monkeypatch
):
    monkeypatch.setattr(Settings, "INCREMENTAL_DEPLOY", False)
    app_state.graph = MagicMock(nodes={"DHCP1": 1, "WEB1": 2, "SW1": 3})
    body = _preview(client, "SW1", "NEW1")
    assert body["incremental"] is False
    assert body["removed"] == ["DHCP1", "WEB1"]
    assert body["bundle_nodes"] == ["NEW1", "SW1"]


def test_an_incremental_host_removes_nothing(client, app_state, monkeypatch):
    monkeypatch.setattr(Settings, "INCREMENTAL_DEPLOY", True)
    app_state.graph = MagicMock(nodes={"DHCP1": 1})
    assert _preview(client, "NEW1")["removed"] == []


def test_the_preview_works_without_a_loaded_topology(client, app_state, monkeypatch):
    monkeypatch.setattr(Settings, "INCREMENTAL_DEPLOY", False)
    app_state.graph = None
    assert _preview(client, "NEW1")["removed"] == []


def test_a_garbage_bundle_gives_an_empty_preview_not_an_error(client, app_state):
    response = client.post(
        "/project/import/preview", files={"file": ("x.topoproj", b"not a zip")}
    )
    assert response.status_code == 200
    assert response.json()["bundle_nodes"] == []


def test_the_import_asks_before_deleting_even_with_disruption_warnings_off():
    js = read_app_js()
    assert 'fetch("/project/import/preview"' in js
    assert 'confirmLabel: "Delete and import", danger: true' in js


def test_menubar_messages_can_be_dismissed_with_a_small_x():
    js = read_app_js()
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    assert 'close.className = "menubar-dismiss"' in js
    assert 'close.addEventListener("click", clearMenubarStatus)' in js
    assert ".menubar-dismiss" in css
