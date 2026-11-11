"""Tests the /live/stage/* HTTP endpoints (app/routes/staging.py) through the test client."""

from __future__ import annotations

from unittest.mock import patch

import app.main as main_module
from app import staging


def test_start_staging_requires_a_live_topology(client, app_state):
    app_state.graph = None
    resp = client.post("/live/stage")
    assert resp.status_code == 503


def test_start_staging_clones_the_live_topology(client, app_state):
    resp = client.post("/live/stage")
    assert resp.status_code == 200
    assert client.get("/live/stage").json() == {"active": True}

    names = {n["name"] for n in client.get("/live/stage/topology").json()}
    assert names == {"R1", "SW1", "VM1"}


def test_mutating_endpoints_404_without_an_active_session(client, app_state):
    assert (
        client.post(
            "/live/stage/nodes", json={"name": "PC1", "role": "PC", "image": "VPCS"}
        ).status_code
        == 409
    )
    assert client.delete("/live/stage/nodes/R1").status_code == 409
    assert client.get("/live/stage/diff").status_code == 409


def test_stage_add_and_remove_node(client, app_state):
    client.post("/live/stage")
    resp = client.post(
        "/live/stage/nodes", json={"name": "PC1", "role": "PC", "image": "VPCS"}
    )
    assert resp.status_code == 200

    diff = client.get("/live/stage/diff").json()
    assert diff["added_nodes"] == [{"name": "PC1", "role": "PC", "image": "VPCS"}]

    assert client.delete("/live/stage/nodes/VM1").status_code == 200
    diff = client.get("/live/stage/diff").json()
    assert diff["removed_nodes"] == ["VM1"]


def test_discard_drops_the_session(client, app_state):
    client.post("/live/stage")
    client.post("/live/stage/discard")
    assert client.get("/live/stage").json() == {"active": False}
    assert client.get("/live/stage/diff").status_code == 409


def test_apply_requires_explicit_confirmation(client, app_state):
    client.post("/live/stage")
    resp = client.post("/live/stage/apply", json={})
    assert resp.status_code == 400


def test_apply_with_no_changes_is_a_no_op(client, app_state):
    client.post("/live/stage")
    resp = client.post("/live/stage/apply", json={"confirm": True})
    assert resp.status_code == 200
    assert resp.json() == {"results": [], "applied": True}
    assert client.get("/live/stage").json() == {"active": False}


def test_apply_runs_staged_changes_and_discards_on_success(client, app_state):
    client.post("/live/stage")
    client.delete("/live/stage/nodes/VM1")

    with patch("app.topology_write.remove_node") as fake_remove_node:
        resp = client.post("/live/stage/apply", json={"confirm": True})

    fake_remove_node.assert_called_once()
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is True
    assert body["results"] == [
        {"action": "remove_node", "target": "VM1", "ok": True, "error": None}
    ]
    assert client.get("/live/stage").json() == {"active": False}


def test_apply_keeps_the_session_open_on_failure(client, app_state):
    client.post("/live/stage")
    client.delete("/live/stage/nodes/VM1")

    with patch("app.topology_write.remove_node", side_effect=RuntimeError("boom")):
        resp = client.post("/live/stage/apply", json={"confirm": True})

    assert resp.status_code == 200
    assert resp.json()["applied"] is False
    # Staging stays open so the operator can see what still needs fixing.
    assert client.get("/live/stage").json() == {"active": True}


def test_is_active_reflects_module_state_directly(app_state):
    assert staging.is_active(main_module.app) is False
    staging.start(main_module.app)
    assert staging.is_active(main_module.app) is True
