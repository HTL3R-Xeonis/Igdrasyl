"""Tests app/project_snapshots.py directly - the storage layer behind the /design/project/{id}/snapshots/* endpoints."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app import design, project_library, project_snapshots


@pytest.fixture(autouse=True)
def library_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_PROJECTS_DIR", str(tmp_path / "projects"))


@pytest.fixture
def project():
    return project_library.create("Office lab")


def _state_with_one_node() -> design.DesignState:
    state = design.new_state()
    design.add_node(state, "PC1", "PC", "VPCS")
    return state


def test_create_list_and_restore_round_trip(project):
    state = _state_with_one_node()
    snapshot = project_snapshots.create(project["id"], "before cleanup", state)
    assert snapshot["name"] == "before cleanup"
    assert "created_at" in snapshot

    listed = project_snapshots.list_snapshots(project["id"])
    assert [s["id"] for s in listed] == [snapshot["id"]]

    restored = project_snapshots.restore(project["id"], snapshot["id"])
    assert set(restored.nodes) == {"PC1"}


def test_list_is_empty_for_a_project_with_no_snapshots(project):
    assert project_snapshots.list_snapshots(project["id"]) == []


def test_list_and_create_404_for_an_unknown_project():
    with pytest.raises(FileNotFoundError):
        project_snapshots.list_snapshots("0" * 32)
    with pytest.raises(FileNotFoundError):
        project_snapshots.create("0" * 32, "x", design.new_state())


def test_create_rejects_a_blank_name(project):
    with pytest.raises(ValueError):
        project_snapshots.create(project["id"], "   ", design.new_state())


def test_restore_404s_for_an_unknown_snapshot(project):
    with pytest.raises(FileNotFoundError):
        project_snapshots.restore(project["id"], "0" * 32)


def test_restore_never_calls_the_template_service(project):
    snapshot = project_snapshots.create(project["id"], "s1", _state_with_one_node())
    with patch(
        "src.graph.environment.Environment.get_environment",
        side_effect=AssertionError("should not be called"),
    ):
        project_snapshots.restore(project["id"], snapshot["id"])


def test_delete_removes_a_snapshot(project):
    snapshot = project_snapshots.create(project["id"], "s1", design.new_state())
    project_snapshots.delete(project["id"], snapshot["id"])
    assert project_snapshots.list_snapshots(project["id"]) == []
    with pytest.raises(FileNotFoundError):
        project_snapshots.delete(project["id"], snapshot["id"])


def test_oldest_snapshot_is_dropped_past_the_cap(project, monkeypatch):
    monkeypatch.setattr(project_snapshots, "MAX_SNAPSHOTS_PER_PROJECT", 2)
    first = project_snapshots.create(project["id"], "first", design.new_state())
    project_snapshots.create(project["id"], "second", design.new_state())
    project_snapshots.create(project["id"], "third", design.new_state())

    ids = {s["id"] for s in project_snapshots.list_snapshots(project["id"])}
    assert first["id"] not in ids
    assert len(ids) == 2


# --- /design/project/{id}/snapshots/* endpoints ---


def test_create_list_restore_and_delete_via_http(client, app_state, project):
    resp = client.post(
        f"/design/project/{project['id']}/snapshots", json={"name": "checkpoint 1"}
    )
    assert resp.status_code == 201
    snapshot = resp.json()["snapshot"]
    assert snapshot["name"] == "checkpoint 1"

    listed = client.get(f"/design/project/{project['id']}/snapshots").json()
    assert [s["id"] for s in listed["snapshots"]] == [snapshot["id"]]

    app_state.design.nodes["PC1"] = design.DesignNode("PC1", "PC", "VPCS", "ON_GNS3")
    resp = client.post(
        f"/design/project/{project['id']}/snapshots/{snapshot['id']}/restore"
    )
    assert resp.status_code == 200
    # Restoring replaces the in-memory sketch with the snapshot's own (empty) one.
    assert "PC1" not in app_state.design.nodes

    resp = client.delete(f"/design/project/{project['id']}/snapshots/{snapshot['id']}")
    assert resp.status_code == 200
    assert client.get(f"/design/project/{project['id']}/snapshots").json() == {
        "snapshots": []
    }


def test_snapshot_endpoints_404_for_an_unknown_project(client, app_state):
    unknown = "0" * 32
    assert client.get(f"/design/project/{unknown}/snapshots").status_code == 404
    assert (
        client.post(
            f"/design/project/{unknown}/snapshots", json={"name": "x"}
        ).status_code
        == 404
    )


def test_create_snapshot_400s_for_a_blank_name(client, app_state, project):
    resp = client.post(
        f"/design/project/{project['id']}/snapshots", json={"name": "   "}
    )
    assert resp.status_code == 400


def test_restore_snapshot_404s_for_an_unknown_snapshot(client, app_state, project):
    resp = client.post(f"/design/project/{project['id']}/snapshots/{'0' * 32}/restore")
    assert resp.status_code == 404
