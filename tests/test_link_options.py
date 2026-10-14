from unittest.mock import MagicMock, patch

import pytest

import app.main as main_module
from app import link_options

_LIVE_NODES = [{"name": "R1", "node_id": "n1"}, {"name": "SW1", "node_id": "n2"}]


def _link(**extra):
    return {
        "link_id": "link-1",
        "capturing": False,
        "nodes": [
            {"node_id": "n1", "label": {"text": "eth0"}},
            {"node_id": "n2", "label": {"text": "Ethernet0"}},
        ],
        **extra,
    }


def test_filters_leave_out_everything_that_is_off():
    assert link_options.filters_from_options(0, 0, 0, 0) == {}
    assert link_options.filters_from_options(50, 5, 10, 1) == {
        "delay": [50, 5],
        "packet_loss": [10],
        "corrupt": [1],
    }


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((-1, 0, 0, 0), "delay"),
        ((10_001, 0, 0, 0), "delay"),
        ((0, 0, 101, 0), "packet loss"),
        ((0, 0, 0, 101), "corruption"),
        ((0, 5, 0, 0), "jitter needs a delay"),
    ],
)
def test_filters_reject_out_of_range_values(args, message):
    with pytest.raises(ValueError, match=message):
        link_options.filters_from_options(*args)


def test_options_are_read_back_from_a_gns3_link():
    link = _link(
        filters={"delay": [20, 3], "packet_loss": [7], "bpf": ["icmp"]}, suspend=True
    )
    assert link_options.options_from_link(link) == {
        "delay_ms": 20,
        "jitter_ms": 3,
        "loss_percent": 7,
        "corrupt_percent": 0,
        "suspended": True,
        "other_filters": ["bpf"],
    }
    assert link_options.options_from_link(_link())["delay_ms"] == 0


def _patched(app_state, link):
    app_state.project_id = "proj-1"
    app_state.gns3_vm_ip = "10.20.20.233"
    return (
        patch(
            "app.gns3_state.GNS3Connection.list_project_nodes", return_value=_LIVE_NODES
        ),
        patch("app.capture.GNS3Connection.list_project_links", return_value=[link]),
        patch("app.link_options.GNS3Connection.ensure_project_open"),
    )


def test_set_options_puts_the_filters_and_keeps_unmanaged_ones(app_state):
    nodes, links, opened = _patched(
        app_state, _link(filters={"bpf": ["icmp"], "delay": [1, 0]})
    )
    answer = MagicMock()
    answer.json.return_value = _link(
        filters={"bpf": ["icmp"], "packet_loss": [10]}, suspend=True
    )
    with (
        nodes,
        links,
        opened,
        patch("app.link_options.requests.put", return_value=answer) as put,
    ):
        result = link_options.set_options(
            main_module.app,
            "R1",
            "SW1",
            delay_ms=0,
            jitter_ms=0,
            loss_percent=10,
            corrupt_percent=0,
            suspended=True,
        )
    url = put.call_args.args[0]
    assert url.endswith("/projects/proj-1/links/link-1")
    assert put.call_args.kwargs["json"] == {
        "filters": {"packet_loss": [10], "bpf": ["icmp"]},
        "suspend": True,
    }
    assert result["loss_percent"] == 10
    assert result["suspended"] is True


def test_endpoint_validates_before_touching_gns3(client):
    with patch("app.link_options.requests.put") as put:
        response = client.put("/links/R1/SW1/options", json={"loss_percent": 500})
    assert response.status_code == 400
    put.assert_not_called()


def test_endpoint_404s_when_there_is_no_live_link(client, app_state):
    nodes, links, opened = _patched(app_state, _link())
    with nodes, links, opened:
        response = client.put("/links/R1/NOPE/options", json={"delay_ms": 5})
    assert response.status_code == 404
