import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from src.graph.environment import Environment

from app import ai_snippets, config_snippets

_TOKEN = "t" * 32
_GENERATOR = "https://generator.test"
_URL = "/config-snippets/ai"
_VLAN = {"kind": "vlan", "vlan_id": 30, "name": "Lab"}
_REQUEST = {"device": "SW1", "prompt": "VLAN 30 Lab"}


@pytest.fixture
def enable_ai(app_state, monkeypatch):
    ai_snippets.set_enabled(True)
    monkeypatch.setattr(ai_snippets, "list_node_ports", lambda *_: [{"name": "Gi0/1"}])
    monkeypatch.setenv("XEONIS_AI_SNIPPETS_TOKEN", _TOKEN)
    monkeypatch.setenv("TOPOLOGYOPERATOR_AI_SNIPPET_GENERATOR_URL", _GENERATOR)
    monkeypatch.setattr(app_state, "project_id", "test-project")
    monkeypatch.setattr(app_state, "push_job", None, raising=False)
    monkeypatch.setattr(app_state, "connectivity_job", None, raising=False)
    monkeypatch.setattr(app_state, "ai_snippet_drafts", {}, raising=False)
    monkeypatch.setitem(
        app_state.graph.nodes,
        "SW1",
        SimpleNamespace(
            image="Cisco IOSvL2 15.2", role="SWITCH", env=Environment.ON_GNS3
        ),
    )


def _draft(client, settings=_VLAN):
    reply = Mock(status_code=200)
    reply.json.return_value = [settings]
    with patch("app.ai_snippets.requests.post", return_value=reply) as upstream:
        response = client.post(f"{_URL}/generate", json=_REQUEST)
    return response, upstream


def test_generation_and_target_selection_require_server_release(client, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_AI_SNIPPET_TOPICS", "vlan")
    with patch("app.ai_snippets.requests.post") as upstream:
        assert client.get(f"{_URL}/status").json()["enabled"] is False
        assert client.get(f"{_URL}/targets").status_code == 403
        assert client.post(f"{_URL}/generate", json=_REQUEST).status_code == 403
    upstream.assert_not_called()


def test_push_runs_only_after_confirmation_and_only_once(client, enable_ai):
    assert client.get(f"{_URL}/targets").json() == ["R1", "SW1"]
    with patch("app.push_jobs.push_all", return_value=[]) as push:
        response, upstream = _draft(client)
        assert response.status_code == 200, response.text
        assert upstream.call_args.args == (_GENERATOR + "/api/config-draft",)
        assert upstream.call_args.kwargs["headers"] == {
            "Authorization": f"Bearer {_TOKEN}"
        }
        assert upstream.call_args.kwargs["allow_redirects"] is False
        assert upstream.call_args.kwargs["json"] == {
            "prompt": "VLAN 30 Lab",
            "context": {
                "device": "SW1",
                "role": "SWITCH",
                "image": "Cisco IOSvL2 15.2",
                "platform": "iosvl2",
                "ports": ["GigabitEthernet0/1"],
                "links": [
                    {"port": "Ethernet0", "neighbor": "R1", "neighbor_port": "gi0/0"}
                ],
            },
        }
        draft = response.json()
        assert draft["device"] == "SW1"
        assert draft["commands"] == [
            "configure terminal",
            "vlan 30",
            "name Lab",
            "exit",
            "end",
        ]
        push.assert_not_called()
        assert config_snippets.list_snippets() == []
        payload = {"draft_id": draft["draft_id"]}
        assert (
            client.post(f"{_URL}/push", json={**payload, "device": "R1"}).status_code
            == 422
        )
        started = client.post(f"{_URL}/push", json=payload)
        assert started.status_code == 202, started.text
        assert client.post(f"{_URL}/push", json=payload).status_code == 409
        path = f"/config-push/jobs/{started.json()['job_id']}"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if client.get(path).json()["status"] == "done":
                break
            time.sleep(0.01)
        else:
            pytest.fail("push job did not finish")
    push.assert_called_once()
    assert push.call_args.args[0] == {"SW1": draft["commands"]}
    assert [row["note"] for row in client.get("/config-push/audit").json()] == [
        "AI snippet"
    ]


@pytest.mark.parametrize(
    "settings",
    [
        {**_VLAN, "vlan_id": True},
        {**_VLAN, "vlan_id": 1002},
        {**_VLAN, "name": "Lab\nend"},
        {**_VLAN, "commands": ["unexpected"]},
    ],
)
def test_invalid_model_values_cannot_become_commands(
    client, enable_ai, app_state, settings
):
    response, _ = _draft(client, settings)
    assert response.status_code == 422
    assert app_state.ai_snippet_drafts == {}


def test_generation_rejects_unknown_device_and_obsolete_topic_field(client, enable_ai):
    with patch("app.ai_snippets.requests.post") as upstream:
        assert (
            client.post(
                f"{_URL}/generate", json={**_REQUEST, "device": "unknown"}
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"{_URL}/generate", json={**_REQUEST, "topic": "interface_description"}
            ).status_code
            == 422
        )
    upstream.assert_not_called()


def test_changed_project_or_revoked_release_blocks_push(
    client, app_state, enable_ai, user_client
):
    response, _ = _draft(client)
    payload = {"draft_id": response.json()["draft_id"]}
    with patch("app.push_jobs.start_job") as push:
        assert user_client.post(f"{_URL}/push", json=payload).status_code == 409
        app_state.project_id = "changed-project"
        try:
            assert client.post(f"{_URL}/push", json=payload).status_code == 409
        finally:
            app_state.project_id = "test-project"
        ai_snippets.set_enabled(False)
        assert client.post(f"{_URL}/push", json=payload).status_code == 403
    push.assert_not_called()


def test_description_and_access_vlan_require_a_known_physical_port():
    port = "GigabitEthernet0/1"
    description = {
        "kind": "description",
        "interface": port,
        "description": "Uplink Raum 2",
    }
    access = {"kind": "access_vlan", "interface": port, "vlan_id": 30}
    assert ai_snippets._compile([description, access], [port], "iosvl2") == [
        "configure terminal",
        "interface GigabitEthernet0/1",
        "description Uplink Raum 2",
        "exit",
        "interface GigabitEthernet0/1",
        "switchport mode access",
        "switchport access vlan 30",
        "exit",
        "end",
    ]
    with pytest.raises(ai_snippets.InvalidDraft):
        ai_snippets._compile([description], ["GigabitEthernet0/2"], "iosvl2")
    description["interface"] = "range GigabitEthernet0/1 - 4"
    with pytest.raises(ai_snippets.InvalidDraft):
        ai_snippets._compile([description], [port], "iosvl2")


def test_ipv4_settings_require_iosv_and_a_valid_cidr():
    port = "GigabitEthernet0/1"
    settings = {"kind": "ipv4", "interface": port, "address": "192.0.2.4/26"}
    commands = ai_snippets._compile([settings], [port], "iosv")
    assert "ip address 192.0.2.4 255.255.255.192" in commands
    with pytest.raises(ai_snippets.InvalidDraft):
        ai_snippets._compile([settings], [port], "iosvl2")
    settings["address"] = "192.0.2.400/26"
    with pytest.raises(ai_snippets.InvalidDraft):
        ai_snippets._compile([settings], [port], "iosv")


def test_missing_fixed_generator_url_keeps_the_feature_disabled(
    client, enable_ai, monkeypatch
):
    monkeypatch.delenv("TOPOLOGYOPERATOR_AI_SNIPPET_GENERATOR_URL")
    assert client.get(f"{_URL}/status").json() == {"enabled": True, "available": False}
    assert client.post(f"{_URL}/generate", json=_REQUEST).status_code == 503


def test_classroom_mode_blocks_older_ai_fix_endpoint(client, monkeypatch):
    monkeypatch.setenv("XEONIS_CLASSROOM_MODE", "1")
    response = client.post("/ai/suggest-fix", json={"device": "R1", "check": "test"})
    assert response.status_code == 403
