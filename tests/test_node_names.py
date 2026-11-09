import pytest

from app import node_names


@pytest.mark.parametrize(
    "name", ["R1", "SW-ACC1", "web_01", "a" * 63, "9lives", "R-CORE"]
)
def test_ordinary_device_names_are_accepted(name):
    node_names.validate(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "  ",
        "a b",
        "x/y",
        "..",
        "../etc",
        "<script>",
        "R1;rm",
        "R1\n",
        "nul\x00l",
        "-lead",
        "_lead",
        "ä",
        "😀",
        "a" * 64,
    ],
)
def test_names_that_break_vm_folders_urls_or_commands_are_refused(name):
    with pytest.raises(ValueError):
        node_names.validate(name)


def test_a_name_differing_only_in_case_counts_as_taken():
    with pytest.raises(ValueError, match="already exists"):
        node_names.validate("r1", ["R1", "SW1"])
    node_names.validate("R2", ["R1", "SW1"])


def test_the_design_canvas_and_the_live_endpoint_answer_400_for_a_bad_name(client):
    body = {"name": "../etc", "role": "server", "image": "debian"}
    assert client.post("/design/nodes", json=body).status_code == 400
    assert client.post("/nodes", json=body).status_code == 400
    ok = client.post("/design/nodes", json={**body, "name": "WEB1"})
    assert ok.status_code == 200
    assert (
        client.post("/design/nodes", json={**body, "name": "web1"}).status_code == 400
    )
