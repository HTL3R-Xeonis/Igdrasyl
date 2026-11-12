import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from src.graph.environment import Environment

from app import ai_snippets, config_snippets

_TOKEN = "t" * 32
_GENERATOR = "https://generator.test"
_URL = "/config-snippets/ai"
_VLAN = {"vlan_id": 30, "name": "Lab"}
_REQUEST = {"topic": "vlan", "device": "SW1", "prompt": "VLAN 30 Lab"}


@pytest.fixture
def enable_vlan(app_state, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_AI_SNIPPET_TOPICS", "vlan,invalid")
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
    reply.json.return_value = settings
    with patch("app.ai_snippets.requests.post", return_value=reply) as upstream:
        response = client.post(f"{_URL}/generate", json=_REQUEST)
    return response, upstream


def test_generation_and_target_selection_require_server_release(client, monkeypatch):
    monkeypatch.delenv("TOPOLOGYOPERATOR_AI_SNIPPET_TOPICS", raising=False)
    with patch("app.ai_snippets.requests.post") as upstream:
        assert client.get(f"{_URL}/topics").json() == []
        assert client.get(f"{_URL}/targets?topic=vlan").status_code == 403
        assert client.post(f"{_URL}/generate", json=_REQUEST).status_code == 403
    upstream.assert_not_called()


def test_push_runs_only_after_confirmation_and_only_once(client, enable_vlan):
    assert client.get(f"{_URL}/targets?topic=vlan").json() == ["SW1"]
    with patch("app.push_jobs.push_all", return_value=[]) as push:
        response, upstream = _draft(client)
        assert response.status_code == 200, response.text
        assert upstream.call_args.args == (
            _GENERATOR + "/api/config-snippets/generate",
        )
        assert upstream.call_args.kwargs["headers"] == {
            "Authorization": f"Bearer {_TOKEN}"
        }
        assert upstream.call_args.kwargs["allow_redirects"] is False
        assert upstream.call_args.kwargs["json"] == {
            "topic": "vlan",
            "prompt": "VLAN 30 Lab",
        }
        draft = response.json()
        assert draft["device"] == "SW1"
        assert draft["commands"] == ["configure terminal", "vlan 30", "name Lab", "end"]
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
        {"vlan_id": True, "name": "Lab"},
        {"vlan_id": 1002, "name": "Lab"},
        {"vlan_id": 30, "name": "Lab\nend"},
        {**_VLAN, "commands": ["unexpected"]},
    ],
)
def test_invalid_model_values_cannot_become_commands(
    client, enable_vlan, app_state, settings
):
    response, _ = _draft(client, settings)
    assert response.status_code == 422
    assert app_state.ai_snippet_drafts == {}


def test_vlan_exercise_rejects_router_and_unreleased_topic(client, enable_vlan):
    with patch("app.ai_snippets.requests.post") as upstream:
        assert (
            client.post(
                f"{_URL}/generate", json={**_REQUEST, "device": "R1"}
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"{_URL}/generate", json={**_REQUEST, "topic": "interface_description"}
            ).status_code
            == 403
        )
    upstream.assert_not_called()


def test_changed_project_or_revoked_release_blocks_push(
    client, app_state, enable_vlan, monkeypatch
):
    response, _ = _draft(client)
    payload = {"draft_id": response.json()["draft_id"]}
    with patch("app.push_jobs.start_job") as push:
        app_state.project_id = "changed-project"
        try:
            assert client.post(f"{_URL}/push", json=payload).status_code == 409
        finally:
            app_state.project_id = "test-project"
        monkeypatch.delenv("TOPOLOGYOPERATOR_AI_SNIPPET_TOPICS")
        assert client.post(f"{_URL}/push", json=payload).status_code == 403
    push.assert_not_called()


def test_description_exercise_accepts_only_a_single_physical_interface():
    settings = {"interface": "GigabitEthernet0/1", "description": "Uplink Raum 2"}
    assert ai_snippets.compile_snippet("interface_description", settings).commands == [
        "configure terminal",
        "interface GigabitEthernet0/1",
        "description Uplink Raum 2",
        "end",
    ]
    settings["interface"] = "range GigabitEthernet0/1 - 4"
    with pytest.raises(ai_snippets.InvalidDraft):
        ai_snippets.compile_snippet("interface_description", settings)


def test_missing_fixed_generator_url_keeps_the_feature_disabled(
    client, enable_vlan, monkeypatch
):
    monkeypatch.delenv("TOPOLOGYOPERATOR_AI_SNIPPET_GENERATOR_URL")
    assert client.get(f"{_URL}/topics").json() == []


def test_classroom_mode_blocks_older_ai_fix_endpoint(client, monkeypatch):
    monkeypatch.setenv("XEONIS_CLASSROOM_MODE", "1")
    response = client.post("/ai/suggest-fix", json={"device": "R1", "check": "test"})
    assert response.status_code == 403
