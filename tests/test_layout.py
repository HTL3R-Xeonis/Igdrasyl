import json

import app.layout as layout_mod


def test_load_layout_uses_computed_defaults_when_nothing_saved(graph, tmp_path, monkeypatch):
    """
    compute_node_positions' own docstring claims positions land within
    [0, width] x [0, height], but it's a force-directed layout that can
    overshoot in practice - not this code's bug to fix, so this only
    checks every node gets some real numeric position, not exact bounds.
    """
    monkeypatch.setenv("TOPOLOGYOPERATOR_LAYOUT_FILE", str(tmp_path / "layout.json"))
    result = layout_mod.load_layout(graph.nodes)
    assert set(result) == {"R1", "SW1", "VM1"}
    for pos in result.values():
        assert isinstance(pos["x"], (int, float))
        assert isinstance(pos["y"], (int, float))


def test_save_position_persists_and_is_returned_on_next_load(graph, tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_LAYOUT_FILE", str(tmp_path / "layout.json"))
    layout_mod.save_position("R1", 123.5, 456.5)

    result = layout_mod.load_layout(graph.nodes)
    assert result["R1"] == {"x": 123.5, "y": 456.5}
    # untouched nodes still get a computed default, not crash/omission
    assert "SW1" in result


def test_save_position_does_not_clobber_other_saved_positions(tmp_path, monkeypatch):
    path = tmp_path / "layout.json"
    monkeypatch.setenv("TOPOLOGYOPERATOR_LAYOUT_FILE", str(path))
    layout_mod.save_position("R1", 1, 1)
    layout_mod.save_position("SW1", 2, 2)

    saved = json.loads(path.read_text())
    assert saved == {"R1": {"x": 1, "y": 1}, "SW1": {"x": 2, "y": 2}}


def test_save_positions_writes_every_entry_in_one_pass(tmp_path, monkeypatch):
    path = tmp_path / "layout.json"
    monkeypatch.setenv("TOPOLOGYOPERATOR_LAYOUT_FILE", str(path))
    layout_mod.save_position("VM1", 9, 9)  # pre-existing, unrelated entry

    layout_mod.save_positions({"R1": {"x": 1, "y": 1}, "SW1": {"x": 2, "y": 2}})

    saved = json.loads(path.read_text())
    assert saved == {"VM1": {"x": 9, "y": 9}, "R1": {"x": 1, "y": 1}, "SW1": {"x": 2, "y": 2}}


def test_apply_imported_layout_uses_bundle_position_when_given(graph, tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_LAYOUT_FILE", str(tmp_path / "layout.json"))
    layout_mod.apply_imported_layout(graph.nodes, {"R1": {"x": 42, "y": 84}})

    saved = layout_mod.load_layout(graph.nodes)
    assert saved["R1"] == {"x": 42, "y": 84}


def test_apply_imported_layout_computes_a_default_for_nodes_the_bundle_omits(
    graph, tmp_path, monkeypatch
):
    monkeypatch.setenv("TOPOLOGYOPERATOR_LAYOUT_FILE", str(tmp_path / "layout.json"))
    layout_mod.apply_imported_layout(graph.nodes, {"R1": {"x": 42, "y": 84}})

    saved = layout_mod.load_layout(graph.nodes)
    # SW1/VM1 weren't in the bundle's layout - still got a real position,
    # not left absent/None.
    for name in ("SW1", "VM1"):
        assert isinstance(saved[name]["x"], (int, float))
        assert isinstance(saved[name]["y"], (int, float))


def test_apply_imported_layout_overwrites_a_stale_saved_position(graph, tmp_path, monkeypatch):
    """
    Real bug found live: node names get reused across unrelated
    topology imports (e.g. "R1" in one 3-node test fixture and again in
    an unrelated 8-device topology). Without this, a node's position
    from a STRUCTURALLY DIFFERENT earlier topology stuck around and got
    mixed with freshly-computed positions for that earlier topology's
    other nodes - a canvas with two unrelated coordinate systems
    overlaid, looking scattered and nothing like either real topology.
    apply_imported_layout must always resolve (and overwrite) every
    current node, never leave a pre-existing entry untouched.
    """
    monkeypatch.setenv("TOPOLOGYOPERATOR_LAYOUT_FILE", str(tmp_path / "layout.json"))
    layout_mod.save_position("R1", -999, -999)  # stale, from an unrelated earlier topology

    layout_mod.apply_imported_layout(graph.nodes, {})  # this bundle gives no positions at all

    saved = layout_mod.load_layout(graph.nodes)
    assert saved["R1"] != {"x": -999, "y": -999}
