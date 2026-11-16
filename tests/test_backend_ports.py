from __future__ import annotations

import pytest

from app import backend_ports


@pytest.fixture(autouse=True)
def _isolated_ports_file(tmp_path, monkeypatch):
    monkeypatch.setenv(backend_ports._PORTS_FILE_ENV_VAR, str(tmp_path / "ports.json"))
    monkeypatch.setenv(backend_ports._PORT_RANGE_ENV_VAR, "9200-9201")
    yield


def test_assign_port_is_stable_across_calls():
    first = backend_ports.assign_port("lab-a.yml")
    second = backend_ports.assign_port("lab-a.yml")
    assert first == second


def test_two_profiles_get_two_different_ports():
    a = backend_ports.assign_port("lab-a.yml")
    b = backend_ports.assign_port("lab-b.yml")
    assert a != b


def test_running_out_of_ports_raises_a_clear_error():
    backend_ports.assign_port("lab-a.yml")
    backend_ports.assign_port(
        "lab-b.yml"
    )  # fills the 2-port range set in the fixture above
    with pytest.raises(backend_ports.NoFreePort):
        backend_ports.assign_port("lab-c.yml")


def test_get_port_is_none_for_an_unassigned_profile():
    assert backend_ports.get_port("never-activated.yml") is None


def test_forget_port_frees_it_up_for_reuse():
    backend_ports.assign_port("lab-a.yml")
    backend_ports.assign_port("lab-b.yml")
    backend_ports.forget_port("lab-a.yml")
    assert backend_ports.get_port("lab-a.yml") is None
    # the freed port is usable again, by the same or another profile
    backend_ports.assign_port("lab-c.yml")


def test_forgetting_a_never_assigned_profile_does_not_raise():
    backend_ports.forget_port("nothing-here.yml")


def test_survives_a_corrupt_store_file(tmp_path, monkeypatch):
    path = tmp_path / "ports.json"
    path.write_text("not json at all")
    monkeypatch.setenv(backend_ports._PORTS_FILE_ENV_VAR, str(path))
    assert backend_ports.all_ports() == {}
    assert backend_ports.assign_port("lab-a.yml") == 9200
