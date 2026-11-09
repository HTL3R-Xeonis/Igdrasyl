"""
Undo/redo of the design sketch (app/design_history.py) and the batch
endpoints it comes with: group move, delete selection, paste.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from src.graph.environment import Environment

import app.main as main_module
from app import design, design_history


def _saved_layout():
    """The positions really stored; GET /design/layout also invents defaults for nodes nobody placed."""
    return main_module.app.state.design.layout


@pytest.fixture(autouse=True)
def _templates():
    Environment._get_templates.cache_clear()
    with (
        patch(
            "src.graph.environment.APIHandler.get_gns3_template_names",
            return_value={"Cisco IOSv 15.6(1)T", "VPCS"},
        ),
        patch(
            "src.graph.environment.APIHandler.get_esxi_template_names",
            return_value={"debian"},
        ),
    ):
        yield
    Environment._get_templates.cache_clear()


def _add(client, name, image="VPCS", role="PC"):
    response = client.post(
        "/design/nodes", json={"name": name, "role": role, "image": image}
    )
    assert response.status_code == 200, response.text


def _names(client):
    return sorted(n["name"] for n in client.get("/design/topology").json())


def test_undo_and_redo_node_add_and_remove(client):
    _add(client, "PC1")
    _add(client, "PC2")
    assert client.get("/design/history").json() == {"can_undo": True, "can_redo": False}

    assert client.post("/design/undo").status_code == 200
    assert _names(client) == ["PC1"]
    assert client.get("/design/history").json() == {"can_undo": True, "can_redo": True}

    client.post("/design/redo")
    assert _names(client) == ["PC1", "PC2"]

    client.delete("/design/nodes/PC1")
    assert _names(client) == ["PC2"]
    client.post("/design/undo")
    assert _names(client) == ["PC1", "PC2"]


def test_new_change_clears_redo(client):
    _add(client, "PC1")
    client.post("/design/undo")
    _add(client, "PC2")
    assert client.get("/design/history").json()["can_redo"] is False
    assert client.post("/design/redo").status_code == 409


def test_undo_with_nothing_to_undo_is_409(client):
    assert client.post("/design/undo").status_code == 409


def test_failed_change_is_not_recorded(client):
    _add(client, "PC1")
    client.post("/design/nodes", json={"name": "PC1", "role": "PC", "image": "VPCS"})
    client.post("/design/undo")
    assert _names(client) == []
    assert client.get("/design/history").json()["can_undo"] is False


def test_undo_restores_links_layout_and_drawings(client):
    _add(client, "PC1")
    _add(client, "PC2")
    client.post(
        "/design/links", json={"node1": "PC1", "if1": "e0", "node2": "PC2", "if2": "e0"}
    )
    client.post("/design/layout", json={"name": "PC1", "x": 10, "y": 20})
    client.post("/design/drawings", json={"drawings": [{"id": 1, "type": "rect"}]})

    client.post("/design/undo")
    assert client.get("/design/drawings").json() == []
    client.post("/design/undo")
    assert _saved_layout().get("PC1") is None
    client.post("/design/undo")
    nodes = {n["name"]: n for n in client.get("/design/topology").json()}
    assert nodes["PC1"]["interfaces"] == {}
    client.post("/design/redo")
    nodes = {n["name"]: n for n in client.get("/design/topology").json()}
    assert nodes["PC1"]["interfaces"] == {"e0": "PC2"}


def test_undo_of_reset_brings_the_sketch_back(client):
    _add(client, "PC1")
    client.post("/design/reset")
    assert _names(client) == []
    client.post("/design/undo")
    assert _names(client) == ["PC1"]


def test_undo_works_while_the_template_service_is_down(client):
    _add(client, "PC1")
    _add(client, "PC2")
    with patch(
        "src.graph.environment.APIHandler.get_gns3_template_names",
        side_effect=TimeoutError("down"),
    ):
        assert client.post("/design/undo").status_code == 200
    assert _names(client) == ["PC1"]


def test_history_is_capped(client, app_state):
    _add(client, "PC1")
    for i in range(design_history.MAX_STEPS + 20):
        client.post("/design/layout", json={"name": "PC1", "x": i, "y": 0})
    history = app_state.design_history
    assert len(history.undo) == design_history.MAX_STEPS


def test_group_move_is_one_undo_step(client):
    _add(client, "PC1")
    _add(client, "PC2")
    client.post(
        "/design/layout/batch",
        json={"positions": {"PC1": {"x": 1, "y": 2}, "PC2": {"x": 3, "y": 4}}},
    )
    layout = client.get("/design/layout").json()
    assert layout["PC2"] == {"x": 3, "y": 4}
    client.post("/design/undo")
    assert _saved_layout().get("PC2") is None


def test_group_move_rejects_unknown_node_without_changes(client):
    _add(client, "PC1")
    response = client.post(
        "/design/layout/batch",
        json={"positions": {"PC1": {"x": 1, "y": 2}, "NOPE": {"x": 3, "y": 4}}},
    )
    assert response.status_code == 404
    assert _saved_layout().get("PC1") is None


def test_delete_selection_is_atomic_and_one_step(client):
    for name in ("A", "B", "C"):
        _add(client, name)
    client.post(
        "/design/links", json={"node1": "A", "if1": "e0", "node2": "B", "if2": "e0"}
    )
    client.post(
        "/design/links", json={"node1": "B", "if1": "e1", "node2": "C", "if2": "e0"}
    )

    bad = client.post("/design/delete", json={"nodes": ["A", "GHOST"]})
    assert bad.status_code == 404
    assert _names(client) == ["A", "B", "C"]

    ok = client.post("/design/delete", json={"nodes": ["A"], "links": [["B", "C"]]})
    assert ok.status_code == 200
    assert _names(client) == ["B", "C"]
    nodes = {n["name"]: n for n in client.get("/design/topology").json()}
    assert nodes["B"]["interfaces"] == {} and nodes["C"]["interfaces"] == {}
    client.post("/design/undo")
    assert _names(client) == ["A", "B", "C"]


def test_paste_adds_nodes_links_and_positions_in_one_step(client):
    payload = {
        "nodes": [
            {"name": "PC1", "role": "PC", "image": "VPCS", "x": 10, "y": 20},
            {"name": "PC2", "role": "PC", "image": "VPCS", "x": 30, "y": 40},
        ],
        "links": [{"node1": "PC1", "if1": "e0", "node2": "PC2", "if2": "e0"}],
    }
    assert client.post("/design/paste", json=payload).status_code == 200
    assert _names(client) == ["PC1", "PC2"]
    assert client.get("/design/layout").json()["PC2"] == {"x": 30, "y": 40}
    client.post("/design/undo")
    assert _names(client) == []


def test_failed_paste_changes_nothing(client):
    _add(client, "PC1")
    payload = {
        "nodes": [
            {"name": "PC2", "role": "PC", "image": "VPCS", "x": 1, "y": 1},
            {"name": "PC1", "role": "PC", "image": "VPCS", "x": 2, "y": 2},
        ],
        "links": [],
    }
    response = client.post("/design/paste", json=payload)
    assert response.status_code == 400
    assert _names(client) == ["PC1"]
    assert _saved_layout() == {}
    client.post("/design/undo")
    assert _names(client) == []


def test_paste_rolls_back_when_a_link_is_invalid(client):
    payload = {
        "nodes": [{"name": "PC1", "role": "PC", "image": "VPCS", "x": 1, "y": 1}],
        "links": [{"node1": "PC1", "if1": "e0", "node2": "GHOST", "if2": "e0"}],
    }
    assert client.post("/design/paste", json=payload).status_code == 400
    assert _names(client) == []


def test_live_group_move_saves_all_positions_at_once(client, monkeypatch, tmp_path):
    monkeypatch.setenv("TOPOLOGYOPERATOR_LAYOUT_FILE", str(tmp_path / "layout.json"))
    first = next(iter(client.get("/topology").json()))["name"]
    response = client.post(
        "/layout/batch", json={"positions": {first: {"x": 5, "y": 6}}}
    )
    assert response.status_code == 200
    assert client.get("/layout").json()[first] == {"x": 5, "y": 6}
    assert (
        client.post(
            "/layout/batch", json={"positions": {"nope": {"x": 1, "y": 1}}}
        ).status_code
        == 404
    )


def test_design_module_remove_many_ignores_a_link_removed_with_its_node():
    state = design.new_state()
    for name in ("A", "B"):
        design.add_node(state, name, "PC", "VPCS")
    design.add_link(state, "A", "e0", "B", "e0")
    design.remove_many(state, ["A"], [("A", "B")])
    assert list(state.nodes) == ["B"]
