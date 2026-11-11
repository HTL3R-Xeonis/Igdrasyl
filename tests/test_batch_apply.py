"""
Tests app/design.py's from_graph and app/batch_apply.py's
compute_diff/apply_diff - the pure logic behind live-edit staging
(app/staging.py, POST /live/stage/*). No real GNS3/ESXi anywhere here;
apply_diff is tested against patched app/topology_write.py functions.
"""

from __future__ import annotations

from unittest.mock import patch

from app import batch_apply, design


def test_from_graph_mirrors_the_live_topology(graph):
    state = design.from_graph(graph)
    assert set(state.nodes) == {"R1", "SW1", "VM1"}
    assert state.nodes["R1"].role == "ROUTER"
    assert state.nodes["R1"].image == "Cisco IOSv 15.6(1)T"
    assert state.nodes["VM1"].environment == "ON_ESXI"
    assert ("R1", "gi0/0", "SW1", "Ethernet0") in state.links
    assert state.nodes["R1"].interfaces["gi0/0"] == "SW1"
    # Every node got a layout position (auto_layout), nothing left piled at the origin.
    assert set(state.layout) == set(state.nodes)


def test_from_graph_never_calls_the_template_service(graph):
    with patch(
        "src.graph.environment.Environment.get_environment",
        side_effect=AssertionError("should not be called"),
    ):
        design.from_graph(graph)


def test_compute_diff_is_empty_for_an_unmodified_clone(graph):
    state = design.from_graph(graph)
    diff = batch_apply.compute_diff(state, graph)
    assert diff.is_empty()


def test_compute_diff_detects_added_and_removed_nodes(graph):
    state = design.from_graph(graph)
    design.remove_node(state, "VM1")
    design.add_node(state, "PC1", "PC", "VPCS")

    diff = batch_apply.compute_diff(state, graph)
    assert diff.removed_nodes == ["VM1"]
    assert diff.added_nodes == [{"name": "PC1", "role": "PC", "image": "VPCS"}]


def test_compute_diff_detects_added_and_removed_links(graph):
    state = design.from_graph(graph)
    design.remove_link(state, "R1", "SW1")
    design.add_node(state, "PC1", "PC", "VPCS")
    design.add_link(state, "R1", "gi0/1", "PC1", "eth0")

    diff = batch_apply.compute_diff(state, graph)
    assert diff.removed_links == [("R1", "gi0/0", "SW1", "Ethernet0")]
    assert diff.added_links == [("R1", "gi0/1", "PC1", "eth0")]


def test_apply_diff_runs_steps_in_dependency_order(app_state):
    diff = batch_apply.Diff(
        added_nodes=[{"name": "PC1", "role": "PC", "image": "VPCS"}],
        removed_nodes=["VM1"],
        added_links=[("R1", "gi0/1", "PC1", "eth0")],
        removed_links=[("R1", "gi0/0", "SW1", "Ethernet0")],
    )
    calls = []
    with (
        patch(
            "app.topology_write.remove_link",
            side_effect=lambda *a: calls.append(("remove_link", a)),
        ),
        patch(
            "app.topology_write.remove_node",
            side_effect=lambda *a: calls.append(("remove_node", a)),
        ),
        patch(
            "app.topology_write.add_node",
            side_effect=lambda *a: calls.append(("add_node", a)),
        ),
        patch(
            "app.topology_write.add_link",
            side_effect=lambda *a: calls.append(("add_link", a)),
        ),
    ):
        results = batch_apply.apply_diff(app_state, diff)

    assert [action for action, _ in calls] == [
        "remove_link",
        "remove_node",
        "add_node",
        "add_link",
    ]
    assert all(result["ok"] for result in results)


def test_apply_diff_stops_at_the_first_failure(app_state):
    diff = batch_apply.Diff(
        added_nodes=[{"name": "PC1", "role": "PC", "image": "VPCS"}],
        removed_nodes=["VM1"],
    )
    with (
        patch("app.topology_write.remove_node", side_effect=RuntimeError("boom")),
        patch("app.topology_write.add_node") as fake_add_node,
    ):
        results = batch_apply.apply_diff(app_state, diff)

    assert len(results) == 1
    assert results[0] == {
        "action": "remove_node",
        "target": "VM1",
        "ok": False,
        "error": "boom",
    }
    fake_add_node.assert_not_called()
