from app import design, design_store


def test_load_returns_none_when_no_file_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_DESIGN_FILE", str(tmp_path / "design_state.json"))
    assert design_store.load() is None


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_DESIGN_FILE", str(tmp_path / "design_state.json"))
    state = design.new_state()
    design.add_node(state, "R1", "ROUTER", "Cisco IOSv 15.6(1)T")
    state.layout["R1"] = {"x": 3.0, "y": 4.0}

    design_store.save(state)
    restored = design_store.load()

    assert restored is not None
    assert restored.nodes["R1"].image == "Cisco IOSv 15.6(1)T"
    assert restored.layout == {"R1": {"x": 3.0, "y": 4.0}}


def test_load_ignores_a_corrupt_file(tmp_path, monkeypatch):
    path = tmp_path / "design_state.json"
    path.write_text("not valid json")
    monkeypatch.setenv("TOPOLOGYOPERATOR_DESIGN_FILE", str(path))

    assert design_store.load() is None
