"""
"Hold" (app/holds.py): parking a project, resuming it, and the guards around
deploying / deleting while one is parked. ESXi, GNS3 and the health-check pass
are mocked - the point is the sequence and the protection of the VMs.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

import app.main as main_module
from app import holds, project_library

PROJECT_ID = "a" * 32


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_HOLDS_FILE", str(tmp_path / "holds.json"))
    monkeypatch.setenv("TOPOLOGYOPERATOR_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("TOPOLOGYOPERATOR_SETTINGS", str(tmp_path / "host.yml"))
    (tmp_path / "host.yml").write_text("esxi:\n  ip: 192.0.2.1\n")


def _live(app_state):
    app_state.project_name = "Office"
    app_state.project_id = "gns3-1"
    app_state.gns3_vm_ip = "10.0.0.5"
    vm = MagicMock(name="vm")
    vm.config.annotation = "topologybuilder-image:debian"
    app_state.esxi_connection.get_vm.return_value = vm
    app_state.esxi_connection.is_vm_powered_on.return_value = True
    return vm


def _vm_node_names(app_state):
    return [n.name for n in app_state.graph.nodes.values() if n.env.name == "ON_ESXI"]


def test_hold_parks_vms_closes_the_project_and_protects_them(app_state):
    vm = _live(app_state)
    with patch("app.holds.GNS3Connection.post") as post:
        result = holds.hold_project(main_module.app, PROJECT_ID, "host.yml")

    esxi = app_state.esxi_connection
    assert result["state"] == "held" and result["vm_count"] == len(
        _vm_node_names(app_state)
    )
    esxi.power_off_vm.assert_called_with(vm)
    assert post.call_args.args[0].endswith("/v2/projects/gns3-1/close")
    annotation = esxi.set_vm_annotation.call_args.args[1]
    assert annotation == (
        f"topologyoperator-hold:{PROJECT_ID}\ntopologybuilder-image:debian"
    )
    held_name = esxi.rename_vm.call_args.args[1]
    assert held_name == f"{_vm_node_names(app_state)[0]} [hold {PROJECT_ID[:8]}]"
    assert app_state.project_id is None
    assert "on hold" in app_state.startup_warning
    # The protection TopologyBuilder's cleanup relies on: it only ever touches
    # VMs whose annotation STARTS with this marker.
    assert not annotation.startswith("topologybuilder-image:")
    assert holds.is_held(PROJECT_ID)


def test_hold_without_a_live_project_is_refused(app_state):
    _live(app_state)
    app_state.project_id = None
    with pytest.raises(holds.HoldError, match="no live GNS3 project"):
        holds.hold_project(main_module.app, PROJECT_ID, "host.yml")
    assert not holds.all_entries()


def test_a_second_hold_is_refused(app_state):
    _live(app_state)
    with patch("app.holds.GNS3Connection.post"):
        holds.hold_project(main_module.app, PROJECT_ID, "host.yml")
        app_state.project_id = "gns3-1"
        with pytest.raises(holds.HoldError, match="already on hold"):
            holds.hold_project(main_module.app, PROJECT_ID, "host.yml")


def _held(app_state):
    _live(app_state)
    with patch("app.holds.GNS3Connection.post"):
        holds.hold_project(main_module.app, PROJECT_ID, "host.yml")


def test_resume_renames_back_opens_gns3_and_runs_the_fix_pass(app_state):
    _held(app_state)
    esxi = app_state.esxi_connection
    name = _vm_node_names(app_state)[0]
    held_vm = MagicMock(name="held-vm")

    def get_vm(requested):
        return held_vm if requested.startswith(f"{name} [hold") else None

    esxi.get_vm.side_effect = get_vm
    esxi.rename_vm.reset_mock()
    esxi.set_vm_annotation.reset_mock()
    with (
        patch("app.holds.resolve_project_id", return_value="gns3-1"),
        patch("app.holds.GNS3Connection.ensure_project_open") as opened,
        patch(
            "app.holds.maintenance.run_verify", return_value={"passed": 3, "total": 3}
        ) as verify,
    ):
        report = holds.resume_project(main_module.app, PROJECT_ID)

    assert report == {"passed": 3, "total": 3}
    esxi.rename_vm.assert_called_once_with(held_vm, name)
    esxi.set_vm_annotation.assert_called_once_with(
        held_vm, "topologybuilder-image:debian"
    )
    opened.assert_called_once()
    verify.assert_called_once_with(main_module.app, fix=True)
    assert app_state.project_id == "gns3-1" and app_state.startup_warning is None
    assert not holds.all_entries()


def test_resume_refuses_when_the_project_is_not_the_selected_one(app_state):
    _held(app_state)
    app_state.project_name = "Another"
    with pytest.raises(holds.HoldError, match="Select this project"):
        holds.resume_project(main_module.app, PROJECT_ID)
    assert holds.is_held(PROJECT_ID)


def test_resume_refuses_when_an_original_name_is_taken(app_state):
    _held(app_state)
    # every name resolves to some VM, i.e. both the held one and the original exist
    app_state.esxi_connection.get_vm.side_effect = lambda n: MagicMock()
    with (
        patch("app.holds.resolve_project_id", return_value="gns3-1"),
        pytest.raises(holds.HoldError, match="already exists"),
    ):
        holds.resume_project(main_module.app, PROJECT_ID)
    assert holds.is_held(PROJECT_ID)


def test_resume_of_something_not_on_hold_is_refused(app_state):
    with pytest.raises(holds.HoldError, match="not on hold"):
        holds.resume_project(main_module.app, PROJECT_ID)


def test_registry_never_exposes_annotations():
    record = {
        "name": "Office",
        "host": "host.yml",
        "state": "held",
        "vms": [{"name": "A", "held_name": "A [hold]", "annotation": "secret-ish"}],
    }
    assert "annotation" not in json.dumps(holds.public(record))


def test_health_reports_the_held_project_and_stops_offering_deploy(client, app_state):
    _held(app_state)
    app_state.project_id = None
    app_state.gns3_vm_ip = "10.0.0.5"
    body = client.get("/health").json()
    assert body["held_project"]["name"] == "Office"
    assert body["deployable_project"] is None


def test_deploying_a_project_on_hold_is_refused(client, app_state):
    _held(app_state)
    preview = {
        "host": {"active": True},
        "selected": True,
        "project": {"name": "Office"},
    }
    with patch("app.main.project_library.deployment_preview", return_value=preview):
        response = client.post(f"/projects/{PROJECT_ID}/deploy/host.yml")
    assert response.status_code == 409
    assert "resume" in response.json()["detail"]


def test_hold_and_resume_endpoints_map_errors(client, app_state):
    _live(app_state)
    selection = {"project_id": PROJECT_ID, "project_name": "Office"}
    with (
        patch("app.main.project_selection.get_selection", return_value=selection),
        patch("app.holds.GNS3Connection.post"),
    ):
        assert client.post("/project/hold").status_code == 200
        assert client.post("/project/hold").status_code == 409  # already on hold
    assert client.post(f"/projects/{'b' * 32}/resume").status_code == 409


# ---- deleting library projects ----


def test_delete_removes_the_project_and_clears_the_designer_link(client, app_state):
    project = project_library.create("Scratch")
    app_state.design_project_id = project["id"]
    response = client.delete(f"/projects/{project['id']}")
    assert response.status_code == 200
    assert client.get("/projects").json()["projects"] == []
    assert app_state.design_project_id is None


def test_delete_is_refused_for_a_project_selected_by_a_host(client):
    project = project_library.create("In use")
    with patch(
        "app.main.project_selection.hosts_using", return_value=["lab.yml", "b.yml"]
    ):
        response = client.delete(f"/projects/{project['id']}")
    assert response.status_code == 409
    assert "lab.yml" in response.json()["detail"]
    assert client.get("/projects").json()["projects"]


def test_delete_is_refused_for_a_project_on_hold(client, app_state):
    project = project_library.create("Parked")
    _live(app_state)
    with patch("app.holds.GNS3Connection.post"):
        holds.hold_project(main_module.app, project["id"], "host.yml")
    response = client.delete(f"/projects/{project['id']}")
    assert response.status_code == 409
    assert "on hold" in response.json()["detail"]


def test_listing_marks_held_projects(client, app_state):
    project = project_library.create("Parked")
    _live(app_state)
    with patch("app.holds.GNS3Connection.post"):
        holds.hold_project(main_module.app, project["id"], "host.yml")
    listed = client.get("/projects").json()["projects"]
    assert listed[0]["held"] is True


def test_delete_of_an_unknown_project_is_404(client):
    assert client.delete(f"/projects/{'c' * 32}").status_code == 404


def test_resume_pushes_the_saved_configs_when_the_project_wants_that(client, app_state):
    project = project_library.create("Pushy")
    project_library.save_configs(project["id"], {"R1": ["show version"]}, True)
    _live(app_state)
    with patch("app.holds.GNS3Connection.post"):
        holds.hold_project(main_module.app, project["id"], "host.yml")
    app_state.esxi_connection.get_vm.side_effect = lambda n: (
        MagicMock() if "[hold" in n else None
    )
    fake_result = MagicMock()
    fake_result.model_dump.return_value = {"device": "R1", "ok": True}
    with (
        patch("app.holds.resolve_project_id", return_value="gns3-1"),
        patch("app.holds.GNS3Connection.ensure_project_open"),
        patch(
            "app.holds.maintenance.run_verify", return_value={"passed": 1, "total": 1}
        ),
        patch("app.main.push_all_waiting", return_value=[fake_result]) as push,
    ):
        response = client.post(f"/projects/{project['id']}/resume")

    assert response.status_code == 200
    push.assert_called_once()
    assert push.call_args.args[0] == {"R1": ["show version"]}
    assert response.json()["push_results"] == [{"device": "R1", "ok": True}]


def test_resume_pushes_nothing_without_the_switch(client, app_state):
    project = project_library.create("Quiet")
    project_library.save_configs(project["id"], {"R1": ["show version"]}, False)
    _live(app_state)
    with patch("app.holds.GNS3Connection.post"):
        holds.hold_project(main_module.app, project["id"], "host.yml")
    app_state.esxi_connection.get_vm.side_effect = lambda n: (
        MagicMock() if "[hold" in n else None
    )
    with (
        patch("app.holds.resolve_project_id", return_value="gns3-1"),
        patch("app.holds.GNS3Connection.ensure_project_open"),
        patch(
            "app.holds.maintenance.run_verify", return_value={"passed": 1, "total": 1}
        ),
        patch("app.main.push_all_waiting") as push,
    ):
        response = client.post(f"/projects/{project['id']}/resume")
    assert response.status_code == 200
    push.assert_not_called()
    assert "push_results" not in response.json()
