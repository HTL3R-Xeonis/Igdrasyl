import os

import pytest

from app import settings_profiles


@pytest.fixture(autouse=True)
def _isolated_profiles_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(
        settings_profiles._PROFILES_DIR_ENV_VAR, str(tmp_path / "profiles")
    )
    monkeypatch.delenv(settings_profiles._SETTINGS_ENV_VAR, raising=False)
    yield


def test_save_profile_rejects_unsafe_filenames():
    with pytest.raises(settings_profiles.InvalidProfileName):
        settings_profiles.save_profile("../evil.yml", b"esxi:\n  ip: 1.2.3.4\n")


def test_save_profile_rejects_non_yaml_extension():
    with pytest.raises(settings_profiles.InvalidProfileName):
        settings_profiles.save_profile("host.txt", b"esxi:\n  ip: 1.2.3.4\n")


def test_save_profile_rejects_invalid_yaml():
    with pytest.raises(settings_profiles.InvalidProfileName):
        settings_profiles.save_profile("host.yml", b"esxi: [unclosed")


def test_save_profile_then_list_shows_a_preview():
    settings_profiles.save_profile(
        "host-a.yml",
        b"topology_file: ./topo.yaml\nesxi:\n  ip: 10.0.0.1\n  gns3_vm_name: GNS3-VM\ngns3:\n  project_name: myproj\n",
    )

    profiles = settings_profiles.list_profiles()

    assert len(profiles) == 1
    assert profiles[0]["filename"] == "host-a.yml"
    assert profiles[0]["esxi_ip"] == "10.0.0.1"
    assert profiles[0]["gns3_vm_name"] == "GNS3-VM"
    assert "topology_file" not in profiles[0]
    assert "project_name" not in profiles[0]
    assert profiles[0]["active"] is False


def test_list_profiles_marks_the_one_matching_topologyoperator_settings(
    monkeypatch, tmp_path
):
    settings_profiles.save_profile("host-a.yml", b"esxi:\n  ip: 10.0.0.1\n")
    settings_profiles.save_profile("host-b.yml", b"esxi:\n  ip: 10.0.0.2\n")
    active_path = settings_profiles._profiles_dir() / "host-b.yml"
    monkeypatch.setenv(settings_profiles._SETTINGS_ENV_VAR, str(active_path))

    profiles = {p["filename"]: p for p in settings_profiles.list_profiles()}

    assert profiles["host-a.yml"]["active"] is False
    assert profiles["host-b.yml"]["active"] is True


def test_list_profiles_reports_an_error_for_unparseable_files_instead_of_crashing():
    path = settings_profiles._profiles_dir() / "broken.yml"
    path.write_text("esxi: [unclosed")

    profiles = settings_profiles.list_profiles()

    assert len(profiles) == 1
    assert profiles[0]["error"] is not None


def test_delete_profile_removes_the_file():
    settings_profiles.save_profile("host-a.yml", b"esxi:\n  ip: 10.0.0.1\n")

    settings_profiles.delete_profile("host-a.yml")

    assert settings_profiles.list_profiles() == []


def test_delete_profile_raises_for_unknown_file():
    with pytest.raises(FileNotFoundError):
        settings_profiles.delete_profile("nope.yml")


def test_delete_profile_rejects_unsafe_filenames():
    with pytest.raises(settings_profiles.InvalidProfileName):
        settings_profiles.delete_profile("../../etc/passwd")


def test_profile_path_raises_for_unknown_file():
    with pytest.raises(FileNotFoundError):
        settings_profiles.profile_path("nope.yml")


def test_restart_into_profile_raises_for_unknown_file_without_scheduling_anything():
    with pytest.raises(FileNotFoundError):
        settings_profiles.restart_into_profile("nope.yml")


def test_restart_into_profile_schedules_a_delayed_restart(monkeypatch):
    settings_profiles.save_profile("host-a.yml", b"esxi:\n  ip: 10.0.0.1\n")
    calls = []
    monkeypatch.setattr(
        settings_profiles, "_restart_with_settings", lambda path: calls.append(path)
    )
    monkeypatch.setattr(settings_profiles, "_RESTART_DELAY_SECONDS", 0.01)

    settings_profiles.restart_into_profile("host-a.yml")

    import time

    time.sleep(0.1)
    assert len(calls) == 1
    assert calls[0].endswith("host-a.yml")


def test_restart_with_settings_reexecs_with_the_env_var_set(monkeypatch):
    captured = {}

    def fake_execve(path, argv, env):
        captured["path"] = path
        captured["argv"] = argv
        captured["env"] = env

    monkeypatch.setattr(os, "execve", fake_execve)

    settings_profiles._restart_with_settings("/some/path/host-a.yml")

    assert (
        captured["env"][settings_profiles._SETTINGS_ENV_VAR] == "/some/path/host-a.yml"
    )
