"""A host profile must name its service addresses itself (or the environment must) - no silent lab defaults."""

from __future__ import annotations

import pytest
from src.settings import Settings

from app import profile_secrets, required_settings

ENV_NAMES = (
    "ESXI_TEMPLATE_SERVER_URL",
    "GNS3_TEMPLATE_SERVER_URL",
    "TOPOLOGY_GENERATOR_URL",
    "OVA_BASE_URL",
)
FULL = (
    "esxi:\n  ip: 10.0.0.1\napi:\n  esxi_template_server_url: http://a\n  gns3_template_server_url: http://b\n"
    "  topology_generator_url: http://c\n  ova_base_url: http://d\n"
)


@pytest.fixture
def no_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def write(tmp_path, text, name="host.yml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_a_complete_profile_passes(no_env, tmp_path):
    assert required_settings.missing_api_settings(write(tmp_path, FULL)) == []
    required_settings.check(write(tmp_path, FULL))


def test_the_missing_keys_are_named(no_env, tmp_path):
    path = write(
        tmp_path, "esxi:\n  ip: 10.0.0.1\napi:\n  esxi_template_server_url: http://a\n"
    )
    assert required_settings.missing_api_settings(path) == [
        "gns3_template_server_url",
        "topology_generator_url",
        "ova_base_url",
    ]
    with pytest.raises(required_settings.MissingProfileSettings) as raised:
        required_settings.check(path)
    text = str(raised.value)
    assert "host.yml" in text and "api.ova_base_url" in text
    assert "not used as a fallback" in text


def test_the_environment_counts_as_setting_it(no_env, tmp_path, monkeypatch):
    path = write(tmp_path, "esxi:\n  ip: 10.0.0.1\n")
    monkeypatch.setenv("OVA_BASE_URL", "http://env:80")
    assert "ova_base_url" not in required_settings.missing_api_settings(path)
    assert len(required_settings.missing_api_settings(path)) == 3


def test_an_unreadable_profile_is_left_to_the_normal_loading(tmp_path):
    assert required_settings.missing_api_settings(tmp_path / "nope.yml") == []


def test_activating_a_profile_without_its_addresses_is_refused(gateway_client, no_env):
    resp = gateway_client.post(
        "/settings-profiles",
        files={"file": ("lab.yml", b"esxi:\n  ip: 10.0.0.2\n", "text/yaml")},
    )
    assert resp.status_code == 200
    resp = gateway_client.post("/settings-profiles/lab.yml/activate")
    assert resp.status_code == 400
    assert "api.ova_base_url" in resp.json()["detail"]


def test_stored_passwords_win_and_the_environment_ones_are_cleared(monkeypatch):
    monkeypatch.setattr(Settings.ESXI, "PASSWORD", "from-the-environment")
    monkeypatch.setattr(Settings.GNS3, "PASSWORD", "from-the-environment")
    problems = profile_secrets.drop_environment_passwords("lab.yml", [])
    assert Settings.ESXI.PASSWORD is None and Settings.GNS3.PASSWORD is None
    assert len(problems) == 2
    assert all("from-the-environment" not in p for p in problems)
    assert "ESXi password" in problems[0] and "lab.yml" in problems[0]


def test_nothing_is_cleared_when_the_host_has_its_own_passwords(monkeypatch):
    monkeypatch.setattr(Settings.ESXI, "PASSWORD", "stored")
    monkeypatch.setattr(Settings.GNS3, "PASSWORD", "stored")
    assert (
        profile_secrets.drop_environment_passwords(
            "lab.yml", ["esxi_password", "gns3_password"]
        )
        == []
    )
    assert Settings.ESXI.PASSWORD == "stored" and Settings.GNS3.PASSWORD == "stored"


# --- a profile that cannot start must be refused before the process restarts into it -------------------------------


def test_activatable_needs_valid_yaml(no_env, tmp_path):
    path = write(tmp_path, "esxi: [unclosed\n")
    with pytest.raises(required_settings.MissingProfileSettings) as raised:
        required_settings.check_activatable(path)
    assert "not valid YAML" in str(raised.value) and "host.yml" in str(raised.value)


def test_activatable_needs_a_mapping(no_env, tmp_path):
    path = write(tmp_path, "- just\n- a list\n")
    with pytest.raises(required_settings.MissingProfileSettings) as raised:
        required_settings.check_activatable(path)
    assert "must be a YAML mapping" in str(raised.value)


def test_activatable_names_an_unreadable_file(tmp_path):
    with pytest.raises(required_settings.MissingProfileSettings) as raised:
        required_settings.check_activatable(tmp_path / "gone.yml")
    assert "cannot be read" in str(raised.value)


def test_activatable_names_the_missing_api_keys(no_env, tmp_path):
    path = write(tmp_path, "esxi:\n  ip: 10.0.0.1\n")
    with pytest.raises(required_settings.MissingProfileSettings) as raised:
        required_settings.check_activatable(path)
    assert "api.ova_base_url" in str(raised.value)


def test_a_complete_profile_is_activatable(no_env, tmp_path):
    required_settings.check_activatable(write(tmp_path, FULL))


def test_the_single_process_activate_route_refuses_a_profile_that_cannot_start(
    client, no_env, monkeypatch
):
    from app import settings_profiles

    restarted = []
    monkeypatch.setattr(
        settings_profiles, "restart_into_profile", lambda name: restarted.append(name)
    )
    resp = client.post(
        "/settings-profiles",
        files={"file": ("lab.yml", b"esxi:\n  ip: 10.0.0.2\n", "text/yaml")},
    )
    assert resp.status_code == 200

    resp = client.post("/settings-profiles/lab.yml/activate")

    assert resp.status_code == 400
    assert "api.ova_base_url" in resp.json()["detail"]
    assert restarted == []  # the server was not restarted into a profile it cannot run


# --- the ESXi address must be named too ---------------------------------------------------------------------------------


def test_a_profile_without_an_esxi_address_is_named_not_defaulted_to_the_lab(
    no_env, tmp_path
):
    path = write(
        tmp_path, FULL.replace("esxi:\n  ip: 10.0.0.1\n", "esxi:\n  username: root\n")
    )

    assert required_settings.missing_settings(path) == ["esxi.ip"]
    with pytest.raises(required_settings.MissingProfileSettings) as raised:
        required_settings.check(path)
    text = str(raised.value)
    assert "esxi.ip" in text and "not used as a fallback" in text
    assert (
        "environment variables" not in text
    )  # there is no variable for it, so it is not suggested


def test_an_empty_or_absent_esxi_section_counts_as_missing(no_env, tmp_path):
    for body in ("api:\n  esxi_template_server_url: http://a\n", "esxi:\napi: {}\n"):
        assert "esxi.ip" in required_settings.missing_settings(write(tmp_path, body))


def test_both_kinds_of_missing_setting_are_named_together(no_env, tmp_path):
    path = write(tmp_path, "gns3:\n  username: gns3\n")

    missing = required_settings.missing_settings(path)

    assert missing[0] == "esxi.ip" and "api.ova_base_url" in missing
    assert "environment variables" in required_settings.explain("h.yml", missing)
