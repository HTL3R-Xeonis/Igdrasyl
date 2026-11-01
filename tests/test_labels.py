"""User-moved port and node-name labels: the offset store, the live and design endpoints, and the design sketch's own
copy (undo/redo and removal)."""

import pytest

from app import design, label_layout

# ---------- the offset store ----------


def test_apply_changes_sets_and_replaces_an_offset():
    labels = {}
    label_layout.apply_changes(labels, {"R1|Gi0/0": {"dx": 5, "dy": -3}})
    assert labels == {"R1|Gi0/0": {"dx": 5.0, "dy": -3.0}}
    label_layout.apply_changes(labels, {"R1|Gi0/0": {"dx": 1, "dy": 2}})
    assert labels["R1|Gi0/0"] == {"dx": 1.0, "dy": 2.0}


def test_none_or_a_zero_offset_puts_the_label_back():
    labels = {"R1|Gi0/0": {"dx": 5, "dy": 5}, "R1|": {"dx": 1, "dy": 1}}
    label_layout.apply_changes(labels, {"R1|Gi0/0": None, "R1|": {"dx": 0, "dy": 0}})
    assert labels == {}


def test_a_name_label_has_an_empty_interface_part():
    labels = {}
    label_layout.apply_changes(labels, {"SW1|": {"dx": 0, "dy": 12}})
    assert "SW1|" in labels


def test_offsets_are_clamped():
    labels = {}
    label_layout.apply_changes(labels, {"R1|Gi0/0": {"dx": 1e9, "dy": -1e9}})
    assert labels["R1|Gi0/0"] == {"dx": 4000.0, "dy": -4000.0}


@pytest.mark.parametrize("key", ["R1", "", "|Gi0/0"])
def test_a_key_without_a_node_is_rejected(key):
    with pytest.raises(ValueError, match="not a label key"):
        label_layout.apply_changes({}, {key: {"dx": 1, "dy": 1}})


def test_a_non_finite_offset_is_rejected():
    with pytest.raises(ValueError, match="finite"):
        label_layout.apply_changes({}, {"R1|Gi0/0": {"dx": float("nan"), "dy": 0}})


def test_drop_node_forgets_all_of_its_labels_only():
    labels = {
        "R1|Gi0/0": {"dx": 1, "dy": 1},
        "R1|": {"dx": 1, "dy": 1},
        "R10|Gi0/0": {"dx": 1, "dy": 1},
    }
    label_layout.drop_node(labels, "R1")
    assert list(labels) == ["R10|Gi0/0"]


def test_the_store_round_trips_through_its_file():
    assert label_layout.load() == {}
    label_layout.save_changes({"R1|Gi0/0": {"dx": 4, "dy": 6}})
    label_layout.save_changes({"SW1|": {"dx": 1, "dy": 2}})
    assert label_layout.load() == {
        "R1|Gi0/0": {"dx": 4.0, "dy": 6.0},
        "SW1|": {"dx": 1.0, "dy": 2.0},
    }


def test_an_unreadable_file_means_no_offsets(tmp_path, monkeypatch):
    path = tmp_path / "broken.json"
    path.write_text("{not json")
    monkeypatch.setenv("TOPOLOGYOPERATOR_LABELS_FILE", str(path))
    assert label_layout.load() == {}


# ---------- live endpoints ----------


def test_live_labels_round_trip_and_reset(client, app_state):
    assert client.get("/labels").json() == {}
    resp = client.post("/labels", json={"labels": {"R1|Gi0/0": {"dx": 7, "dy": -4}}})
    assert resp.status_code == 200
    assert client.get("/labels").json() == {"R1|Gi0/0": {"dx": 7.0, "dy": -4.0}}
    client.post("/labels", json={"labels": {"R1|Gi0/0": None}})
    assert client.get("/labels").json() == {}


def test_live_labels_reject_an_unknown_node(client, app_state):
    resp = client.post("/labels", json={"labels": {"GHOST|Gi0/0": {"dx": 1, "dy": 1}}})
    assert resp.status_code == 404


def test_live_labels_reject_a_key_without_a_node(client, app_state):
    resp = client.post("/labels", json={"labels": {"Gi0/0": {"dx": 1, "dy": 1}}})
    assert resp.status_code == 422


def test_live_labels_post_is_503_when_the_graph_failed_to_load(client, app_state):
    app_state.graph = None
    resp = client.post("/labels", json={"labels": {"R1|": {"dx": 1, "dy": 1}}})
    assert resp.status_code == 503


# ---------- the design sketch ----------


def _two_nodes(client):
    client.post("/design/nodes", json={"name": "R1", "role": "ROUTER", "image": "VPCS"})
    client.post(
        "/design/nodes", json={"name": "SW1", "role": "SWITCH", "image": "VPCS"}
    )


def test_design_labels_round_trip(client, app_state):
    _two_nodes(client)
    resp = client.post("/design/labels", json={"labels": {"SW1|": {"dx": 3, "dy": 9}}})
    assert resp.status_code == 200
    assert client.get("/design/labels").json() == {"SW1|": {"dx": 3.0, "dy": 9.0}}


def test_design_labels_reject_an_unknown_node(client, app_state):
    resp = client.post(
        "/design/labels", json={"labels": {"GHOST|": {"dx": 1, "dy": 1}}}
    )
    assert resp.status_code == 404


def test_design_label_moves_can_be_undone_and_redone(client, app_state):
    _two_nodes(client)
    client.post("/design/labels", json={"labels": {"R1|": {"dx": 5, "dy": 5}}})
    client.post("/design/undo")
    assert client.get("/design/labels").json() == {}
    client.post("/design/redo")
    assert client.get("/design/labels").json() == {"R1|": {"dx": 5.0, "dy": 5.0}}


def test_removing_a_design_node_drops_its_labels(client, app_state):
    _two_nodes(client)
    client.post(
        "/design/labels",
        json={"labels": {"R1|": {"dx": 1, "dy": 1}, "SW1|": {"dx": 2, "dy": 2}}},
    )
    client.delete("/design/nodes/R1")
    assert list(client.get("/design/labels").json()) == ["SW1|"]


def test_design_labels_survive_the_save_and_restore_round_trip():
    state = design.new_state()
    state.labels["R1|Gi0/0"] = {"dx": 2.0, "dy": 3.0}
    restored = design.from_dict(design.to_dict(state))
    assert restored.labels == {"R1|Gi0/0": {"dx": 2.0, "dy": 3.0}}


def test_an_old_saved_sketch_without_labels_still_loads():
    assert design.from_dict({"nodes": [], "links": []}).labels == {}
