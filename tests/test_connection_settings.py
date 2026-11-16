import stat
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


def test_editor_preserves_unexposed_settings_and_hides_secrets(profile, gateway_client):
    response = gateway_client.get("/settings-profiles/host.yml/connections")
    assert response.status_code == 200
    assert "secret" not in response.text
    response = gateway_client.put(
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
def test_invalid_update_leaves_file_untouched(profile, gateway_client, fields):
    before = profile.read_bytes()
    assert (
        gateway_client.put(
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


def test_tests_use_unsaved_values_without_writing(profile, gateway_client):
    before = profile.read_bytes()
    with patch(
        "app.connection_settings._probe",
        side_effect=lambda item: {
            "service": item[0],
            "address": item[1],
            "status": "reachable",
        },
    ):
        response = gateway_client.post(
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
def test_invalid_profile_has_clear_error(profile, gateway_client, content):
    profile.write_text(content)
    assert (
        gateway_client.get("/settings-profiles/host.yml/connections").status_code == 400
    )


def test_missing_profile_returns_404(profile, gateway_client):
    assert (
        gateway_client.get("/settings-profiles/missing.yml/connections").status_code
        == 404
    )


def test_builder_defaults_round_trip_and_create_without_activation(
    profile, gateway_client, monkeypatch
):
    response = gateway_client.get("/host-config/schema")
    groups = response.json()["groups"]
    fields = {
        field["key"]: field["default"] for group in groups for field in group["fields"]
    }
    assert len(fields) == 21
    assert "log_file_path" not in fields
    assert "esxi.gns3_vm_ova_path" not in fields
    assert "esxi.gns3_vm_template_id" in fields
    preview = gateway_client.post("/host-config/preview", json={"fields": fields})
    assert preview.status_code == 200, preview.text
    data = yaml.safe_load(preview.json()["yaml"])
    assert data["incremental_deploy"] is True
    assert data["esxi"]["ignore_port_groups"] == ["PG_GNS3_TRUNK"]
    assert data["esxi"]["gns3_vm_mac_range"] is None
    assert "project_name" not in data["gns3"]
    assert "topology_file" not in data
    with patch("app.settings_profiles.restart_into_profile") as restart:
        result = gateway_client.post(
            "/host-config/create", json={"filename": "new-host.yml", "fields": fields}
        )
        assert result.status_code == 200
        restart.assert_not_called()
    assert yaml.safe_load((profile.parent / "new-host.yml").read_text()) == data
    assert (
        gateway_client.post(
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
def test_builder_rejects_invalid_config(gateway_client, fields):
    assert (
        gateway_client.post("/host-config/preview", json={"fields": fields}).status_code
        == 400
    )


def test_builder_rejects_unsafe_filename(profile, gateway_client):
    assert (
        gateway_client.post(
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


def test_entered_passwords_are_stored_apart_and_never_returned(profile, gateway_client):
    secret = "pw-for-test-only"
    response = gateway_client.put(
        "/settings-profiles/host.yml/connections",
        json={"fields": {"esxi.port": "8443"}, "secrets": {"esxi_password": secret}},
    )
    assert response.status_code == 200
    assert secret not in response.text
    assert secret not in profile.read_text()
    stored = profile.parent / ".secrets" / "host.yml.json"
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    body = gateway_client.get("/settings-profiles/host.yml/connections")
    assert secret not in body.text
    assert body.json()["passwords_set"] == {
        "esxi_password": True,
        "gns3_password": False,
    }


def test_blank_password_keeps_the_stored_one(profile):
    from app import profile_secrets

    profile_secrets.save("host.yml", {"esxi_password": "one", "gns3_password": "two"})
    profile_secrets.save("host.yml", {"esxi_password": "", "gns3_password": "three"})
    assert profile_secrets._read("host.yml") == {
        "esxi_password": "one",
        "gns3_password": "three",
    }


@pytest.mark.parametrize(
    "secrets",
    [{"esxi_password": "a\nb"}, {"esxi_password": "x" * 300}, {"other": "x"}],
)
def test_invalid_password_input_changes_nothing(profile, gateway_client, secrets):
    before = profile.read_bytes()
    response = gateway_client.put(
        "/settings-profiles/host.yml/connections",
        json={"fields": {"esxi.port": "9000"}, "secrets": secrets},
    )
    assert response.status_code == 400
    assert profile.read_bytes() == before


def test_apply_overrides_settings_and_delete_removes_them(profile):
    from src.settings import Settings

    from app import profile_secrets

    profile_secrets.save("host.yml", {"gns3_password": "g"})
    old = Settings.GNS3.PASSWORD
    try:
        assert profile_secrets.apply("host.yml") == ["gns3_password"]
        assert Settings.GNS3.PASSWORD == "g"
    finally:
        Settings.GNS3.PASSWORD = old
    profile_secrets.delete("host.yml")
    assert profile_secrets.status("host.yml") == {
        "esxi_password": False,
        "gns3_password": False,
    }


def test_create_host_with_passwords_stores_them(profile, gateway_client):
    response = gateway_client.post(
        "/host-config/create",
        json={
            "filename": "new.yml",
            "fields": {"esxi.ip": "192.0.2.7"},
            "secrets": {"esxi_password": "abc"},
        },
    )
    assert response.status_code == 200
    assert "abc" not in (profile.parent / "new.yml").read_text()
    assert (profile.parent / ".secrets" / "new.yml.json").exists()


@pytest.fixture
def defaults_file(tmp_path, monkeypatch):
    path = tmp_path / "host_defaults.yml"
    monkeypatch.setenv("TOPOLOGYOPERATOR_HOST_DEFAULTS", str(path))
    return path


def _schema_defaults(gateway_client):
    groups = gateway_client.get("/host-config/schema").json()["groups"]
    return {f["key"]: f["default"] for g in groups for f in g["fields"]}


def test_defaults_file_overrides_the_form_defaults(defaults_file, gateway_client):
    before = _schema_defaults(gateway_client)
    defaults_file.write_text(
        "esxi:\n  ip: 192.0.2.50\n  datastore: ds9\napi:\n"
        "  topology_generator_url: http://192.0.2.9:8002\nincremental_deploy: false\n"
    )
    after = _schema_defaults(gateway_client)
    assert after["esxi.ip"] == "192.0.2.50"
    assert after["esxi.datastore"] == "ds9"
    assert after["api.topology_generator_url"] == "http://192.0.2.9:8002"
    assert after["incremental_deploy"] == "false"
    assert after["esxi.port"] == before["esxi.port"]
    assert "ip: 192.0.2.50" in connection_settings.render_config({})


def test_missing_defaults_file_changes_nothing(defaults_file, gateway_client):
    assert not defaults_file.exists()
    assert _schema_defaults(gateway_client)["esxi.port"] == "443"


@pytest.mark.parametrize(
    "content",
    ["esxi: [a, b]\n", "esxi:\n  port: abc\n", "esxi: {ip: 'a/b'}\n", ": : :\n"],
)
def test_invalid_defaults_file_is_ignored_as_a_whole(
    defaults_file, gateway_client, content
):
    baseline = _schema_defaults(gateway_client)
    defaults_file.write_text("api:\n  topology_generator_url: http://192.0.2.9:8002\n")
    assert _schema_defaults(gateway_client) != baseline
    defaults_file.write_text(content)
    assert _schema_defaults(gateway_client) == baseline


def test_passwords_and_unknown_keys_in_defaults_file_are_not_used(
    defaults_file, gateway_client
):
    defaults_file.write_text(
        "esxi:\n  password: hunter2\n  bogus: 1\n  ip: 192.0.2.60\n"
    )
    defaults = connection_settings.effective_defaults()
    assert defaults["esxi.ip"] == "192.0.2.60"
    assert "hunter2" not in str(defaults)
    assert "hunter2" not in gateway_client.get("/host-config/schema").text


def test_existing_profile_still_shows_the_real_default(
    defaults_file, profile, gateway_client
):
    defaults_file.write_text("esxi:\n  port: 9999\n")
    assert (
        gateway_client.get("/settings-profiles/host.yml/connections").json()["fields"][
            "esxi.port"
        ]
        == "443"
    )
