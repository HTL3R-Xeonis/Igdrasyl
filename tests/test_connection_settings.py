from unittest.mock import patch

import pytest
import requests
import yaml

from app import connection_settings, settings_profiles


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR", str(tmp_path))
    monkeypatch.delenv("TOPOLOGYOPERATOR_SETTINGS", raising=False)
    path = tmp_path / "host.yml"
    path.write_text(
        "esxi:\n  ip: 192.0.2.1\n  password: secret\n  ignore_port_groups: [MGMT]\ncustom: keep\n"
    )
    return path


def test_editor_preserves_unexposed_settings_and_hides_secrets(profile, client):
    response = client.get("/settings-profiles/host.yml/connections")
    assert response.status_code == 200
    assert "secret" not in response.text
    response = client.put(
        "/settings-profiles/host.yml/connections",
        json={
            "fields": {
                "api.topology_generator_url": "http://192.0.2.9:8002/",
                "esxi.port": "8443",
            }
        },
    )
    assert response.status_code == 200
    data = yaml.safe_load(profile.read_text())
    assert data["custom"] == "keep"
    assert data["esxi"]["password"] == "secret"
    assert data["esxi"]["ignore_port_groups"] == ["MGMT"]
    assert data["esxi"]["port"] == 8443
    assert data["api"]["topology_generator_url"] == "http://192.0.2.9:8002"


@pytest.mark.parametrize(
    "fields",
    [
        {"esxi.port": "0"},
        {"esxi.port": "abc"},
        {"api.topology_generator_url": "file:///etc/passwd"},
        {"api.topology_generator_url": "http://user:secret@example.com"},
        {"esxi.password": "replacement"},
    ],
)
def test_invalid_update_leaves_file_untouched(profile, client, fields):
    before = profile.read_bytes()
    assert (
        client.put(
            "/settings-profiles/host.yml/connections", json={"fields": fields}
        ).status_code
        == 400
    )
    assert profile.read_bytes() == before


def test_active_external_profile_is_listed_and_edited(profile, tmp_path, monkeypatch):
    external = tmp_path / "external" / "active.yml"
    external.parent.mkdir()
    external.write_text("esxi: {ip: 192.0.2.2}")
    monkeypatch.setenv("TOPOLOGYOPERATOR_SETTINGS", str(external))
    assert any(
        p["filename"] == "active.yml" and p["active"]
        for p in settings_profiles.list_profiles()
    )
    connection_settings.update_fields("active.yml", {"esxi.ip": "192.0.2.3"})
    assert yaml.safe_load(external.read_text())["esxi"]["ip"] == "192.0.2.3"


def test_tests_use_unsaved_values_without_writing(profile, client):
    before = profile.read_bytes()
    with patch(
        "app.connection_settings._probe",
        side_effect=lambda item: {
            "service": item[0],
            "address": item[1],
            "status": "reachable",
        },
    ):
        response = client.post(
            "/settings-profiles/host.yml/connections/test",
            json={"fields": {"api.topology_generator_url": "http://192.0.2.8:8002"}},
        )
    assert response.status_code == 200
    results = response.json()["results"]
    assert (
        next(r for r in results if r["service"] == "Topology generator")["address"]
        == "http://192.0.2.8:8002"
    )
    assert results[-1]["status"] == "skipped"
    assert profile.read_bytes() == before


def test_probe_reports_http_errors_and_timeout():
    with patch("app.connection_settings.requests.get") as get:
        get.return_value.__enter__.return_value.status_code = 503
        result = connection_settings._probe(("Generator", "http://example.com", None))
        assert result["status"] == "warning"
        assert "503" in result["detail"]
        assert get.call_args.kwargs["allow_redirects"] is False
        get.side_effect = requests.Timeout
        assert (
            connection_settings._probe(("Generator", "http://example.com", None))[
                "status"
            ]
            == "error"
        )


@pytest.mark.parametrize("content", ["[wrong]", "esxi: [wrong]", "esxi: [unclosed"])
def test_invalid_profile_has_clear_error(profile, client, content):
    profile.write_text(content)
    assert client.get("/settings-profiles/host.yml/connections").status_code == 400


def test_missing_profile_returns_404(profile, client):
    assert client.get("/settings-profiles/missing.yml/connections").status_code == 404


def test_builder_defaults_round_trip_and_create_without_activation(
    profile, client, monkeypatch
):
    response = client.get("/host-config/schema")
    groups = response.json()["groups"]
    fields = {
        field["key"]: field["default"] for group in groups for field in group["fields"]
    }
    assert len(fields) == 21
    assert "log_file_path" not in fields
    assert "esxi.gns3_vm_ova_path" not in fields
    assert "esxi.gns3_vm_template_id" in fields
    preview = client.post("/host-config/preview", json={"fields": fields})
    assert preview.status_code == 200, preview.text
    data = yaml.safe_load(preview.json()["yaml"])
    assert data["incremental_deploy"] is True
    assert data["esxi"]["ignore_port_groups"] == ["PG_GNS3_TRUNK"]
    assert data["esxi"]["gns3_vm_mac_range"] is None
    assert "project_name" not in data["gns3"]
    assert "topology_file" not in data
    with patch("app.settings_profiles.restart_into_profile") as restart:
        result = client.post(
            "/host-config/create", json={"filename": "new-host.yml", "fields": fields}
        )
        assert result.status_code == 200
        restart.assert_not_called()
    assert yaml.safe_load((profile.parent / "new-host.yml").read_text()) == data
    assert (
        client.post(
            "/host-config/create", json={"filename": "new-host.yml", "fields": fields}
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"generate_max_retries": "-1"},
        {"incremental_deploy": "yes"},
        {"esxi.gns3_vm_mac_range": "00:50:56:00:00:02,00:50:56:00:00:01"},
        {"esxi.gns3_vm_mac_range": "invalid"},
        {"esxi.gns3_vm_template_id": "../invalid"},
    ],
)
def test_builder_rejects_invalid_config(client, fields):
    assert (
        client.post("/host-config/preview", json={"fields": fields}).status_code == 400
    )


def test_builder_rejects_unsafe_filename(profile, client):
    assert (
        client.post(
            "/host-config/create", json={"filename": "../outside.yml", "fields": {}}
        ).status_code
        == 400
    )


def test_optional_and_list_fields_round_trip(profile):
    values = {
        "esxi.ignore_port_groups": "MGMT\nTrunk",
        "gns3.parent_interface": "",
        "esxi.gns3_vm_mac_range": "00:50:56:00:00:01\n00:50:56:00:00:02",
        "incremental_deploy": "false",
    }
    connection_settings.update_fields("host.yml", values)
    fields = connection_settings.read_fields("host.yml")
    assert all(fields[key] == value for key, value in values.items())
