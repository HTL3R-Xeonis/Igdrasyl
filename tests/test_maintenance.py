"""
Tests app/maintenance.py and its /maintenance/* endpoints against a fake
TopologyBackend - a real one opens real ESXi/GNS3 sessions (see
app/topology_write.py). What TopologyBuilder actually detects/repairs is
its own, already-tested concern; these only prove the wiring: the right
backend call with the right arguments, the response shape, and the error
mapping.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.settings import Settings


def _backend() -> MagicMock:
    return MagicMock()


def test_verify_returns_results_and_counts(client, app_state):
    backend = _backend()
    backend.verify.return_value = ([], [(True, "node A: started"), (False, "VM1: off")])

    with patch("app.maintenance.get_or_create_backend", return_value=backend):
        resp = client.post("/maintenance/verify", json={})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "actions": [],
        "results": [
            {"ok": True, "description": "node A: started"},
            {"ok": False, "description": "VM1: off"},
        ],
        "passed": 1,
        "total": 2,
    }
    backend.verify.assert_called_once_with(Settings.TOPOLOGY_FILE, fix=False)


def test_verify_without_a_body_defaults_to_no_fix(client, app_state):
    backend = _backend()
    backend.verify.return_value = ([], [])

    with patch("app.maintenance.get_or_create_backend", return_value=backend):
        resp = client.post("/maintenance/verify")

    assert resp.status_code == 200, resp.text
    backend.verify.assert_called_once_with(Settings.TOPOLOGY_FILE, fix=False)


def test_verify_with_fix_returns_the_applied_repairs(client, app_state):
    backend = _backend()
    backend.verify.return_value = (["Powered on ESXi VM 'VM1'"], [(True, "ok")])

    with patch("app.maintenance.get_or_create_backend", return_value=backend):
        resp = client.post("/maintenance/verify", json={"fix": True})

    assert resp.status_code == 200, resp.text
    assert resp.json()["actions"] == ["Powered on ESXi VM 'VM1'"]
    backend.verify.assert_called_once_with(Settings.TOPOLOGY_FILE, fix=True)


def test_verify_subinterface_repair_only_runs_when_asked_for_explicitly(client, app_state):
    backend = _backend()
    backend.verify.return_value = (["Created VLAN subinterface vlan10"], [(True, "ok")])

    with patch("app.maintenance.get_or_create_backend", return_value=backend):
        resp = client.post("/maintenance/verify", json={"fix": True, "fix_vlan_subinterfaces": True})

    assert resp.status_code == 200, resp.text
    backend.verify.assert_called_once_with(Settings.TOPOLOGY_FILE, fix=True, fix_vlan_subinterfaces=True)


def test_verify_503s_when_the_topology_is_unavailable(client, app_state):
    app_state.graph = None
    app_state.startup_warning = "template service down"

    resp = client.post("/maintenance/verify", json={})

    assert resp.status_code == 503
    assert "template service down" in resp.text


def test_verify_502s_on_a_live_infrastructure_failure(client, app_state):
    backend = _backend()
    backend.verify.side_effect = RuntimeError("ESXi unreachable")

    with patch("app.maintenance.get_or_create_backend", return_value=backend):
        resp = client.post("/maintenance/verify", json={})

    assert resp.status_code == 502
    assert "ESXi unreachable" in resp.text


def test_orphans_lists_what_the_backend_found(client, app_state):
    orphan = {"type": "gns3_node", "name": "LEFTOVER", "reason": "no matching node"}
    backend = _backend()
    backend.find_orphans.return_value = [orphan]

    with patch("app.maintenance.get_or_create_backend", return_value=backend):
        resp = client.get("/maintenance/orphans")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"orphans": [orphan]}
    backend.find_orphans.assert_called_once_with(Settings.TOPOLOGY_FILE)


def test_orphans_503s_when_the_topology_is_unavailable(client, app_state):
    app_state.graph = None
    assert client.get("/maintenance/orphans").status_code == 503


def test_delete_orphans_passes_the_reviewed_list_to_the_backend(client, app_state):
    backend = _backend()
    orphans = [
        {"type": "gns3_node", "name": "LEFTOVER", "reason": "r"},
        {"type": "esxi_vm", "name": "OLD_VM", "reason": "r"},
    ]

    with patch("app.maintenance.get_or_create_backend", return_value=backend):
        resp = client.post("/maintenance/orphans/delete", json={"orphans": orphans})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 2}
    backend.delete_orphans.assert_called_once_with(Settings.TOPOLOGY_FILE, orphans)


def test_delete_orphans_rejects_an_unknown_orphan_type(client, app_state):
    backend = _backend()

    with patch("app.maintenance.get_or_create_backend", return_value=backend):
        resp = client.post(
            "/maintenance/orphans/delete",
            json={"orphans": [{"type": "everything", "name": "x"}]},
        )

    assert resp.status_code == 422
    backend.delete_orphans.assert_not_called()


def test_delete_orphans_502s_on_a_live_infrastructure_failure(client, app_state):
    backend = _backend()
    backend.delete_orphans.side_effect = TimeoutError("GNS3 timed out")

    with patch("app.maintenance.get_or_create_backend", return_value=backend):
        resp = client.post(
            "/maintenance/orphans/delete",
            json={"orphans": [{"type": "gns3_node", "name": "x"}]},
        )

    assert resp.status_code == 502
