"""Notes on nodes: the store, the live and design endpoints, the bundle, and the default logins read from a template."""

import io
import json
import zipfile
from unittest.mock import patch

import pytest
import requests
from frontend_js import read_app_js
from src.settings import Settings

from app import design, node_credentials, node_notes, project_bundle

# ---------- the store ----------


def test_a_note_is_set_replaced_and_removed_by_blank_text():
    notes = {}
    node_notes.apply_note(notes, "R1", "uplink to the ISP")
    node_notes.apply_note(notes, "R1", "changed")
    assert notes == {"R1": "changed"}
    node_notes.apply_note(notes, "R1", "  \n ")
    assert notes == {}


def test_line_endings_are_normalised_and_the_length_is_limited():
    notes = {}
    node_notes.apply_note(notes, "R1", "a\r\nb\rc")
    assert notes["R1"] == "a\nb\nc"
    with pytest.raises(ValueError, match="at most"):
        node_notes.apply_note(notes, "R1", "x" * (node_notes.MAX_LENGTH + 1))


def test_the_store_round_trips_through_its_file():
    assert node_notes.load() == {}
    node_notes.save_note("R1", "one")
    node_notes.save_note("SW1", "two")
    assert node_notes.load() == {"R1": "one", "SW1": "two"}


def test_an_unreadable_or_odd_file_means_no_notes(tmp_path, monkeypatch):
    path = tmp_path / "broken.json"
    monkeypatch.setenv("TOPOLOGYOPERATOR_NOTES_FILE", str(path))
    path.write_text("{nope")
    assert node_notes.load() == {}
    path.write_text(json.dumps({"R1": "ok", "R2": 5, "R3": None}))
    assert node_notes.load() == {"R1": "ok"}


def test_restrict_keeps_only_known_nodes_with_text():
    cleaned = node_notes.restrict(
        {"R1": "x", "GONE": "y", "R2": 3, "R3": " "}, {"R1", "R2", "R3"}
    )
    assert cleaned == {"R1": "x"}


# ---------- live endpoints ----------


def test_live_notes_round_trip_and_clear(client, app_state):
    assert client.get("/notes").json() == {}
    assert client.put("/notes/R1", json={"text": "core router"}).json() == {
        "R1": "core router"
    }
    assert client.get("/notes").json() == {"R1": "core router"}
    client.put("/notes/R1", json={"text": ""})
    assert client.get("/notes").json() == {}


def test_live_note_for_an_unknown_node_is_404(client, app_state):
    assert client.put("/notes/GHOST", json={"text": "x"}).status_code == 404


def test_a_too_long_note_is_rejected(client, app_state):
    resp = client.put("/notes/R1", json={"text": "x" * (node_notes.MAX_LENGTH + 1)})
    assert resp.status_code == 422


def test_live_note_is_503_when_the_graph_failed_to_load(client, app_state):
    app_state.graph = None
    assert client.put("/notes/R1", json={"text": "x"}).status_code == 503


# ---------- the design sketch ----------


def _two_nodes(client):
    client.post("/design/nodes", json={"name": "R1", "role": "ROUTER", "image": "VPCS"})
    client.post(
        "/design/nodes", json={"name": "SW1", "role": "SWITCH", "image": "VPCS"}
    )


def test_design_notes_round_trip_undo_and_node_removal(client, app_state):
    _two_nodes(client)
    client.put("/design/notes/R1", json={"text": "edge"})
    client.put("/design/notes/SW1", json={"text": "access"})
    assert client.get("/design/notes").json() == {"R1": "edge", "SW1": "access"}

    client.post("/design/undo")
    assert client.get("/design/notes").json() == {"R1": "edge"}
    client.post("/design/redo")
    assert client.get("/design/notes").json() == {"R1": "edge", "SW1": "access"}

    client.delete("/design/nodes/R1")
    assert client.get("/design/notes").json() == {"SW1": "access"}


def test_design_note_for_an_unknown_node_is_404(client, app_state):
    assert client.put("/design/notes/GHOST", json={"text": "x"}).status_code == 404


def test_design_notes_survive_the_save_and_restore_round_trip():
    state = design.new_state()
    state.notes["R1"] = "keep me"
    assert design.from_dict(design.to_dict(state)).notes == {"R1": "keep me"}
    assert design.from_dict({"nodes": [], "links": []}).notes == {}


# ---------- the bundle ----------


def test_design_export_carries_the_notes_of_existing_nodes():
    state = design.from_dict(
        {
            "nodes": [
                {
                    "name": "R1",
                    "role": "ROUTER",
                    "image": "VPCS",
                    "environment": "ON_GNS3",
                    "interfaces": {},
                }
            ],
            "links": [],
            "notes": {"R1": "edge", "GONE": "x"},
        }
    )
    data = project_bundle.build_design_export(state, "Lab")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert json.loads(zf.read("notes.json")) == {"notes": {"R1": "edge"}}


def test_live_export_and_import_carry_the_notes(app_state, temp_topology_file):
    import app.main as main_module

    node_notes.save_note("R1", "from the lab")
    exported = project_bundle.build_export(main_module.app)
    with zipfile.ZipFile(io.BytesIO(exported)) as zf:
        assert json.loads(zf.read("notes.json")) == {"notes": {"R1": "from the lab"}}

    node_notes.save_note("R1", "stale")
    node_notes.save_note("SW1", "stale too")

    class _Backend:
        def deploy_topology(self, path, text):
            return None

    app_state.topology_backend = _Backend()
    project_bundle.apply_import(main_module.app, exported)
    assert node_notes.load() == {"R1": "from the lab"}


# ---------- default logins from the template ----------


@pytest.fixture
def real_template_lookups(monkeypatch):
    monkeypatch.setattr(Settings.API, "LITERAL_API_VALUES", False, raising=False)


def _served(templates):
    return patch(
        "app.node_credentials.APIHandler.get", return_value={"templates": templates}
    )


def test_an_esxi_template_gives_its_users_with_their_passwords(real_template_lookups):
    templates = [
        {"name": "Debian 12", "users": ["root", "admin"], "passwords": ["pw-a", "pw-b"]}
    ]
    with _served(templates):
        result = node_credentials.for_image("debian12", "ON_ESXI")
    assert result["source"] == "esxi"
    assert result["logins"] == [
        {"user": "root", "password": "pw-a"},
        {"user": "admin", "password": "pw-b"},
    ]


def test_more_users_than_passwords_leaves_the_rest_without_one(real_template_lookups):
    with _served([{"name": "T", "users": ["a", "b"], "passwords": ["only"]}]):
        logins = node_credentials.for_image("T", "ON_ESXI")["logins"]
    assert logins == [
        {"user": "a", "password": "only"},
        {"user": "b", "password": None},
    ]


def test_a_gns3_template_gives_its_usage_text(real_template_lookups):
    with _served([{"name": "Cisco IOSv", "usage": "  login: admin / no password  "}]):
        result = node_credentials.for_image("Cisco IOSv", "ON_GNS3")
    assert result["source"] == "gns3"
    assert result["usage"] == "login: admin / no password"
    assert result["logins"] == []


def test_an_unknown_image_gives_nothing(real_template_lookups):
    with _served([{"name": "Other", "users": ["x"], "passwords": ["y"]}]):
        assert node_credentials.for_image("Missing", "ON_ESXI")["source"] is None


def test_an_unreachable_template_service_is_reported_not_raised(real_template_lookups):
    with patch(
        "app.node_credentials.APIHandler.get",
        side_effect=requests.exceptions.ConnectionError("down"),
    ):
        result = node_credentials.for_image("T", "ON_ESXI")
    assert result["source"] is None
    assert "could not be reached" in result["error"]
    assert "down" not in result["error"]


def test_the_credentials_endpoints_use_the_nodes_image(client, app_state):
    with patch(
        "app.node_credentials.for_image", return_value={"source": None}
    ) as lookup:
        assert client.get("/nodes/R1/credentials").status_code == 200
    assert lookup.call_args.args[0] == app_state.graph.nodes["R1"].image
    assert client.get("/nodes/GHOST/credentials").status_code == 404
    assert client.get("/design/nodes/GHOST/credentials").status_code == 404


# ---------- the page ----------


def test_the_node_popup_has_a_note_button_and_the_page_a_note_dialog():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "app" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    assert 'id="selected-note-btn"' in html
    assert '<dialog id="note-dialog"' in html and 'id="note-text"' in html


def test_default_logins_are_rendered_without_innerhtml():

    js = read_app_js()
    start = js.index("function renderNodeLogins")
    body = js[start : js.index("async function openNodeNote")]
    assert "innerHTML" not in body  # a template's text must never be parsed as markup
