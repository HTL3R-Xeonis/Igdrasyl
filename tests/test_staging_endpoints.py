"""Tests the /live/stage/* HTTP endpoints (app/routes/staging.py) through the test client."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import app.main as main_module
from app import live_lock, staging


def _apply_reviewed(client):
    preview = client.get("/live/stage/diff").json()
    return client.post(
        "/live/stage/apply", json={"confirm": True, "review_id": preview["review_id"]}
    )


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


def test_apply_without_review_is_rejected(client, app_state):
    client.post("/live/stage")
    response = client.post("/live/stage/apply", json={"confirm": True})
    assert response.status_code == 409
    assert "Review the current" in response.json()["detail"]


def test_apply_with_no_changes_is_a_no_op(client, app_state):
    client.post("/live/stage")
    resp = _apply_reviewed(client)
    assert resp.status_code == 200
    assert resp.json() == {"results": [], "applied": True}
    assert client.get("/live/stage").json() == {"active": False}


def test_apply_runs_staged_changes_and_discards_on_success(client, app_state):
    client.post("/live/stage")
    client.delete("/live/stage/nodes/VM1")

    def verify_exclusive_apply(*_args):
        with live_lock.exclusive("Nested node removal"):
            with live_lock.try_exclusive() as available:
                assert available is False

    with patch(
        "app.topology_write.remove_node", side_effect=verify_exclusive_apply
    ) as fake_remove_node:
        resp = _apply_reviewed(client)

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
        resp = _apply_reviewed(client)

    assert resp.status_code == 200
    assert resp.json()["applied"] is False
    # Staging stays open so the operator can see what still needs fixing.
    assert client.get("/live/stage").json() == {"active": True}


def test_apply_failure_leaves_only_unapplied_changes_for_the_next_review(
    client, app_state
):
    client.post("/live/stage")
    for name in ("PC1", "PC2"):
        assert (
            client.post(
                "/live/stage/nodes", json={"name": name, "role": "PC", "image": "VPCS"}
            ).status_code
            == 200
        )

    def add_node(_app, name, role, image):
        if name == "PC2":
            raise RuntimeError("second node failed")
        graph = app_state.graph
        app_state.graph = SimpleNamespace(
            nodes={**graph.nodes, name: SimpleNamespace(role=role, image=image)},
            edges=graph.edges,
        )

    with patch("app.topology_write.add_node", side_effect=add_node):
        response = _apply_reviewed(client)

    assert response.json()["applied"] is False
    assert [result["ok"] for result in response.json()["results"]] == [True, False]
    preview = client.get("/live/stage/diff")
    assert preview.status_code == 200
    assert preview.json()["added_nodes"] == [
        {"name": "PC2", "role": "PC", "image": "VPCS"}
    ]


def test_apply_rejects_live_topology_changes_after_preview(client, app_state):
    client.post("/live/stage")
    client.delete("/live/stage/nodes/VM1")
    preview = client.get("/live/stage/diff").json()
    graph = app_state.graph
    app_state.graph = SimpleNamespace(nodes=graph.nodes, edges=graph.edges[:-1])
    with patch("app.batch_apply.apply_diff") as apply_diff:
        response = client.post(
            "/live/stage/apply",
            json={"confirm": True, "review_id": preview["review_id"]},
        )
    assert response.status_code == 409
    assert "live topology or selected project changed" in response.json()["detail"]
    assert client.get("/live/stage/diff").status_code == 409
    apply_diff.assert_not_called()


def test_apply_rejects_project_switch_even_with_same_topology(client, app_state):
    client.post("/live/stage")
    preview = client.get("/live/stage/diff").json()
    app_state.project_id = "other-project"
    response = client.post(
        "/live/stage/apply", json={"confirm": True, "review_id": preview["review_id"]}
    )
    assert response.status_code == 409
    assert client.get("/live/stage").json() == {"active": True}


def test_apply_requires_a_fresh_preview_after_the_draft_changes(client, app_state):
    client.post("/live/stage")
    preview = client.get("/live/stage/diff").json()
    client.delete("/live/stage/nodes/VM1")
    with patch("app.batch_apply.apply_diff") as apply_diff:
        response = client.post(
            "/live/stage/apply",
            json={"confirm": True, "review_id": preview["review_id"]},
        )
    assert response.status_code == 409
    assert "Review the current" in response.json()["detail"]
    apply_diff.assert_not_called()


def test_is_active_reflects_module_state_directly(app_state):
    assert staging.is_active(main_module.app) is False
    staging.start(main_module.app)
    assert staging.is_active(main_module.app) is True
