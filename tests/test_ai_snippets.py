from unittest.mock import Mock, patch

import pytest

from app import config_snippets

_TOKEN = "t" * 32
_GENERATOR = "https://generator.test"
_URL = "/config-snippets/ai"
_VLAN = {"vlan_id": 30, "name": "Lab"}


@pytest.fixture
def enable_vlan(monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_AI_SNIPPET_TOPICS", "vlan,invalid")
    monkeypatch.setenv("XEONIS_AI_SNIPPETS_TOKEN", _TOKEN)
    monkeypatch.setenv("TOPOLOGYOPERATOR_AI_SNIPPET_GENERATOR_URL", _GENERATOR)


def test_generation_and_save_require_server_release(client, monkeypatch):
    monkeypatch.delenv("TOPOLOGYOPERATOR_AI_SNIPPET_TOPICS", raising=False)
    monkeypatch.setenv("XEONIS_AI_SNIPPETS_TOKEN", _TOKEN)
    with patch("app.ai_snippets.requests.post") as upstream:
        assert client.get(f"{_URL}/topics").json() == []
        assert (
            client.post(
                f"{_URL}/generate", json={"topic": "vlan", "prompt": "VLAN 30"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"{_URL}/save", json={"topic": "vlan", "settings": _VLAN}
            ).status_code
            == 403
        )
    upstream.assert_not_called()


def test_generate_drafts_then_save_creates_a_new_snippet(client, enable_vlan):
    assert [topic["id"] for topic in client.get(f"{_URL}/topics").json()] == ["vlan"]
    reply = Mock(status_code=200)
    reply.json.return_value = _VLAN
    with patch("app.ai_snippets.requests.post", return_value=reply) as upstream:
        response = client.post(
            f"{_URL}/generate", json={"topic": "vlan", "prompt": "VLAN 30 Lab"}
        )
    assert response.status_code == 200, response.text
    assert upstream.call_args.args == (_GENERATOR + "/api/config-snippets/generate",)
    assert upstream.call_args.kwargs["headers"] == {"Authorization": f"Bearer {_TOKEN}"}
    assert upstream.call_args.kwargs["allow_redirects"] is False
    draft = response.json()
    assert draft["commands"] == ["configure terminal", "vlan 30", "name Lab", "end"]
    assert config_snippets.list_snippets() == []
    payload = {key: draft[key] for key in ("topic", "settings")}
    saved = client.post(f"{_URL}/save", json=payload)
    second = client.post(f"{_URL}/save", json=payload)
    assert saved.status_code == second.status_code == 201
    assert saved.json()["name"] != second.json()["name"]
    assert config_snippets.get(saved.json()["name"]).commands == draft["commands"]
    assert client.get("/config-push/audit").json() == []


@pytest.mark.parametrize(
    "settings",
    [
        {"vlan_id": True, "name": "Lab"},
        {"vlan_id": 1002, "name": "Lab"},
        {"vlan_id": 30, "name": "Lab\nend"},
        {**_VLAN, "commands": ["unexpected"]},
    ],
)
def test_invalid_values_cannot_become_commands(client, enable_vlan, settings):
    reply = Mock(status_code=200)
    reply.json.return_value = settings
    with patch("app.ai_snippets.requests.post", return_value=reply):
        assert (
            client.post(
                f"{_URL}/generate", json={"topic": "vlan", "prompt": "VLAN 30"}
            ).status_code
            == 422
        )
    assert (
        client.post(
            f"{_URL}/save", json={"topic": "vlan", "settings": settings}
        ).status_code
        == 422
    )
    assert config_snippets.list_snippets() == []


def test_one_topic_release_cannot_enable_another(client, enable_vlan):
    with patch("app.ai_snippets.requests.post") as upstream:
        assert (
            client.post(
                f"{_URL}/generate",
                json={"topic": "interface_description", "prompt": "Describe port"},
            ).status_code
            == 403
        )
    upstream.assert_not_called()


def test_description_exercise_accepts_only_a_single_physical_interface(
    client, enable_vlan, monkeypatch
):
    monkeypatch.setenv("TOPOLOGYOPERATOR_AI_SNIPPET_TOPICS", "interface_description")
    settings = {"interface": "GigabitEthernet0/1", "description": "Uplink Raum 2"}
    payload = {"topic": "interface_description", "settings": settings}
    saved = client.post(f"{_URL}/save", json=payload)
    assert saved.status_code == 201
    assert config_snippets.get(saved.json()["name"]).commands == [
        "configure terminal",
        "interface GigabitEthernet0/1",
        "description Uplink Raum 2",
        "end",
    ]
    settings["interface"] = "range GigabitEthernet0/1 - 4"
    assert client.post(f"{_URL}/save", json=payload).status_code == 422


def test_missing_fixed_generator_url_keeps_the_feature_disabled(
    client, enable_vlan, monkeypatch
):
    monkeypatch.delenv("TOPOLOGYOPERATOR_AI_SNIPPET_GENERATOR_URL")
    assert client.get(f"{_URL}/topics").json() == []


def test_classroom_mode_blocks_older_ai_fix_endpoint(client, monkeypatch):
    monkeypatch.setenv("XEONIS_CLASSROOM_MODE", "1")
    response = client.post("/ai/suggest-fix", json={"device": "R1", "check": "test"})
    assert response.status_code == 403
