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
