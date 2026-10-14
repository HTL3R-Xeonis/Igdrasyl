import io
import json
import time
import zipfile
from unittest.mock import MagicMock, patch

import requests
import yaml
from src.settings import Settings

from app.config_push import DeviceResult


def test_health_reports_degraded_state(client, app_state):
    app_state.startup_warning = "GNS3 VM not found"
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["warning"] == "GNS3 VM not found"
    assert body["gns3_project_id"] is None


def test_health_reports_incremental_deploy_setting(client, app_state):
    original = Settings.INCREMENTAL_DEPLOY
    try:
        Settings.INCREMENTAL_DEPLOY = False
        resp = client.get("/health")
        assert resp.json()["incremental_deploy"] is False
    finally:
        Settings.INCREMENTAL_DEPLOY = original


def test_health_reports_gns3_port(client, app_state):
    resp = client.get("/health")
    assert resp.json()["gns3_port"] == Settings.GNS3.PORT


def test_health_never_exposes_the_topology_path(client, app_state):
    original = Settings.TOPOLOGY_FILE
    try:
        Settings.TOPOLOGY_FILE = "/srv/private/projects/topology.yaml"
        response = client.get("/health")
        assert "topology_file" not in response.json()
        assert "/srv/private" not in response.text
    finally:
        Settings.TOPOLOGY_FILE = original


def test_health_redacts_paths_from_warnings(client, app_state):
    app_state.startup_warning = "Could not open /srv/private/topology.yaml"
    response = client.get("/health")
    assert response.json()["warning"] == "Could not open [server file]"


def test_license_serves_the_repo_root_license_file(client, app_state):
    resp = client.get("/license")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "GNU GENERAL PUBLIC LICENSE" in resp.text


def test_reload_gns3_project_closes_and_reopens(client, app_state):
    app_state.project_id = "proj-1"
    app_state.gns3_vm_ip = "10.20.20.231"

    with patch("app.main.reload_project") as fake_reload:
        resp = client.post("/project/reload-gns3")

    assert resp.status_code == 200, resp.text
    fake_reload.assert_called_once_with("10.20.20.231", Settings.GNS3.PORT, "proj-1")


def test_reload_gns3_project_503s_when_no_live_project(client, app_state):
    app_state.project_id = None
    resp = client.post("/project/reload-gns3")
    assert resp.status_code == 503


def test_reload_gns3_project_502s_on_gns3_failure(client, app_state):
    app_state.project_id = "proj-1"
    app_state.gns3_vm_ip = "10.20.20.231"

    with patch("app.main.reload_project", side_effect=requests.ConnectionError("boom")):
        resp = client.post("/project/reload-gns3")

    assert resp.status_code == 502


def test_deploy_gns3_vm_400s_when_unconfigured(client, app_state):
    from app import gns3_vm_deploy_jobs

    with patch(
        "app.main.gns3_vm_deploy_jobs.start_job",
        side_effect=gns3_vm_deploy_jobs.NotConfigured("not configured"),
    ):
        resp = client.post("/gns3-vm/deploy")
    assert resp.status_code == 400


def test_deploy_gns3_vm_409s_when_already_running(client, app_state):
    from app import gns3_vm_deploy_jobs

    with patch(
        "app.main.gns3_vm_deploy_jobs.start_job",
        side_effect=gns3_vm_deploy_jobs.AlreadyRunning("already running"),
    ):
        resp = client.post("/gns3-vm/deploy")
    assert resp.status_code == 409


def test_deploy_gns3_vm_returns_job_id(client, app_state):
    from app import gns3_vm_deploy_jobs

    fake_job = gns3_vm_deploy_jobs.GNS3VMDeployJob(id="job-1")
    with patch("app.main.gns3_vm_deploy_jobs.start_job", return_value=fake_job):
        resp = client.post("/gns3-vm/deploy")
    assert resp.status_code == 200
    assert resp.json() == {"job_id": "job-1"}


def test_deploy_gns3_vm_defaults_to_keeping_the_old_vm(client, app_state):
    from app import gns3_vm_deploy_jobs

    fake_job = gns3_vm_deploy_jobs.GNS3VMDeployJob(id="job-1")
    with patch(
        "app.main.gns3_vm_deploy_jobs.start_job", return_value=fake_job
    ) as fake_start:
        resp = client.post("/gns3-vm/deploy")
    assert resp.status_code == 200
    assert fake_start.call_args.kwargs == {"delete_old_vm": False}


def test_deploy_gns3_vm_forwards_delete_old_vm(client, app_state):
    from app import gns3_vm_deploy_jobs

    fake_job = gns3_vm_deploy_jobs.GNS3VMDeployJob(id="job-1")
    with patch(
        "app.main.gns3_vm_deploy_jobs.start_job", return_value=fake_job
    ) as fake_start:
        resp = client.post("/gns3-vm/deploy", json={"delete_old_vm": True})
    assert resp.status_code == 200
    assert fake_start.call_args.kwargs == {"delete_old_vm": True}


def test_gns3_vm_deploy_status_404s_for_unknown_job(client, app_state):
    app_state.gns3_vm_deploy_job = None
    resp = client.get("/gns3-vm/deploy/nope")
    assert resp.status_code == 404


def test_gns3_vm_deploy_status_reports_running_stage(client, app_state):
    from app import gns3_vm_deploy_jobs

    app_state.gns3_vm_deploy_job = gns3_vm_deploy_jobs.GNS3VMDeployJob(
        id="job-1", status="running", stage="importing OVA"
    )
    resp = client.get("/gns3-vm/deploy/job-1")
    assert resp.status_code == 200
    assert resp.json() == {"status": "running", "stage": "importing OVA"}


def test_gns3_vm_deploy_status_reports_done_with_ip(client, app_state):
    from app import gns3_vm_deploy_jobs

    app_state.gns3_vm_deploy_job = gns3_vm_deploy_jobs.GNS3VMDeployJob(
        id="job-1",
        status="done",
        stage="waiting for an IP address",
        ip_address="10.20.20.233",
    )
    resp = client.get("/gns3-vm/deploy/job-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "done"
    assert body["ip_address"] == "10.20.20.233"


def test_gns3_vm_deploy_status_reports_error(client, app_state):
    from app import gns3_vm_deploy_jobs

    app_state.gns3_vm_deploy_job = gns3_vm_deploy_jobs.GNS3VMDeployJob(
        id="job-1", status="error", error="no IP reported"
    )
    resp = client.get("/gns3-vm/deploy/job-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert body["error"] == "no IP reported"


def test_topology_returns_static_structure_regardless_of_live_state(client, app_state):
    resp = client.get("/topology")
    assert resp.status_code == 200
    by_name = {n["name"]: n for n in resp.json()}
    assert by_name["R1"]["environment"] == "ON_GNS3"
    assert by_name["VM1"]["environment"] == "ON_ESXI"
    assert by_name["R1"]["interfaces"]["gi0/0"] == "SW1"


def test_nodes_merges_live_gns3_data_for_gns3_nodes(client, app_state):
    app_state.project_id = "proj-1"
    live = [
        {
            "name": "R1",
            "node_id": "n1",
            "status": "started",
            "console": 5000,
            "console_type": "telnet",
        }
    ]
    with patch("app.gns3_state.GNS3Connection.list_project_nodes", return_value=live):
        resp = client.get("/nodes")
    assert resp.status_code == 200
    by_name = {n["name"]: n for n in resp.json()}
    assert by_name["R1"]["status"] == "started"
    assert by_name["R1"]["console_type"] == "telnet"


def test_node_ports_returns_real_interfaces_for_gns3_node(client, app_state):
    app_state.project_id = "proj-1"
    live = [
        {
            "name": "R1",
            "node_id": "n1",
            "ports": [{"name": "gi0/0", "adapter_number": 0, "port_number": 0}],
        }
    ]
    with (
        patch("app.gns3_state.GNS3Connection.list_project_nodes", return_value=live),
        patch("app.gns3_state.GNS3Connection.list_project_links", return_value=[]),
    ):
        resp = client.get("/nodes/R1/ports")
    assert resp.status_code == 200
    assert resp.json() == [{"name": "gi0/0", "in_use": False}]


def test_node_ports_404_for_esxi_node(client, app_state):
    app_state.project_id = "proj-1"
    with patch("app.gns3_state.GNS3Connection.list_project_nodes", return_value=[]):
        resp = client.get("/nodes/VM1/ports")
    assert resp.status_code == 404


def test_nodes_resolves_esxi_power_state_independent_of_gns3_project(client, app_state):
    """
    Real regression test for a real bug this session: /nodes used to
    503 its *entire* response whenever no GNS3 project was resolved,
    even though ESXi VM status is a fully independent data source.
    """
    app_state.project_id = None  # no live GNS3 project at all
    fake_vm = MagicMock()
    fake_vm.runtime.powerState = "poweredOn"
    app_state.esxi_connection.get_vm.return_value = fake_vm

    resp = client.get("/nodes")
    assert resp.status_code == 200, resp.text
    by_name = {n["name"]: n for n in resp.json()}
    assert by_name["VM1"]["status"] == "poweredOn"


def test_nodes_esxi_status_none_when_vm_not_found(client, app_state):
    app_state.project_id = None
    app_state.esxi_connection.get_vm.return_value = None

    resp = client.get("/nodes")
    assert resp.status_code == 200
    by_name = {n["name"]: n for n in resp.json()}
    assert by_name["VM1"]["status"] is None


def test_nodes_esxi_status_ignores_a_stale_gns3_cloud_node_entry(client, app_state):
    """
    Real bug found live: an ESXi-hosted node also gets a GNS3-side
    Cloud node wrapper once linked into the topology, and that
    wrapper's own "status" is just "started" for as long as it exists
    - it never tracks the real VM's lifecycle. A VM destroyed
    independently of its Cloud node (e.g. by a non-incremental deploy
    resetting the ESXi vSwitch, which doesn't clean up Cloud nodes)
    used to still report "started" here, since a live GNS3 entry
    existing at all short-circuited the real ESXi lookup entirely.
    """
    app_state.project_id = "proj-1"
    live = [
        {
            "name": "VM1",
            "node_id": "cloud-1",
            "status": "started",
            "console": None,
            "console_type": "none",
        }
    ]
    app_state.esxi_connection.get_vm.return_value = None

    with patch("app.gns3_state.GNS3Connection.list_project_nodes", return_value=live):
        resp = client.get("/nodes")

    assert resp.status_code == 200
    by_name = {n["name"]: n for n in resp.json()}
    assert by_name["VM1"]["status"] is None
    assert by_name["VM1"]["gns3_node_id"] == "cloud-1"


def test_layout_get_returns_a_position_for_every_node(
    client, app_state, tmp_path, monkeypatch
):
    monkeypatch.setenv("TOPOLOGYOPERATOR_LAYOUT_FILE", str(tmp_path / "layout.json"))
    resp = client.get("/layout")
    assert resp.status_code == 200
    assert set(resp.json()) == {"R1", "SW1", "VM1"}


def test_layout_post_then_get_round_trips(client, app_state, tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_LAYOUT_FILE", str(tmp_path / "layout.json"))
    resp = client.post("/layout", json={"name": "R1", "x": 10, "y": 20})
    assert resp.status_code == 200

    resp = client.get("/layout")
    assert resp.json()["R1"] == {"x": 10, "y": 20}


def test_layout_post_rejects_unknown_node(client, app_state, tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_LAYOUT_FILE", str(tmp_path / "layout.json"))
    resp = client.post("/layout", json={"name": "GHOST", "x": 0, "y": 0})
    assert resp.status_code == 404


def test_drawings_get_reflects_current_state(client, app_state):
    app_state.last_drawings = []
    resp = client.get("/drawings")
    assert resp.status_code == 200
    assert resp.json() == []


def test_drawings_post_then_get_round_trips(client, app_state):
    drawing = {
        "id": 1,
        "type": "rect",
        "x1": 0,
        "y1": 0,
        "x2": 100,
        "y2": 50,
        "stroke": "#7cb342",
    }
    resp = client.post("/drawings", json={"drawings": [drawing]})
    assert resp.status_code == 200

    resp = client.get("/drawings")
    assert resp.json() == [drawing]
    assert app_state.last_drawings == [drawing]


def _valid_bundle(
    topology_yaml_text: str,
    layout: dict[str, dict[str, float]] | None = None,
    configs: dict[str, list[str]] | None = None,
    auto_config_push: bool = False,
) -> bytes:
    manifest = {
        "format_version": 1,
        "project_name": "test",
        "exported_at": "now",
        "auto_config_push": auto_config_push,
    }
    settings = {
        "esxi": {"ip": Settings.ESXI.IP, "gns3_vm_name": Settings.ESXI.GNS3_VM_NAME},
        "gns3": {"username": Settings.GNS3.USERNAME, "port": Settings.GNS3.PORT},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("topology.yaml", topology_yaml_text)
        zf.writestr("settings.yml", yaml.safe_dump(settings))
        zf.writestr("configs.json", json.dumps({"devices": configs or {}}))
        if layout is not None:
            zf.writestr("layout.json", json.dumps({"layout": layout}))
    return buf.getvalue()


def test_export_project_returns_zip_with_attachment_header(
    client, app_state, temp_topology_file, tmp_path, monkeypatch
):
    monkeypatch.setenv("TOPOLOGYOPERATOR_LAYOUT_FILE", str(tmp_path / "layout.json"))
    resp = client.get("/project/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert ".topoproj" in resp.headers["content-disposition"]
    assert set(zipfile.ZipFile(io.BytesIO(resp.content)).namelist()) == {
        "manifest.json",
        "topology.yaml",
        "settings.yml",
        "configs.json",
        "drawings.json",
        "layout.json",
    }


def test_export_project_includes_current_node_positions(
    client, app_state, temp_topology_file, tmp_path, monkeypatch
):
    monkeypatch.setenv("TOPOLOGYOPERATOR_LAYOUT_FILE", str(tmp_path / "layout.json"))
    client.post("/layout", json={"name": "R1", "x": 111, "y": 222})

    resp = client.get("/project/export")
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    layout = json.loads(zf.read("layout.json"))["layout"]
    assert layout["R1"] == {"x": 111, "y": 222}


def _wait_for_import_job(client, job_id: str, timeout: float = 2.0) -> dict:
    """
    POST /project/import now only kicks off a background job (see
    app/import_jobs.py) and returns immediately - tests poll this same
    way the frontend does instead of asserting on the POST response
    itself.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/project/import/{job_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.01)
    raise AssertionError(f"import job {job_id!r} still running after {timeout}s")


def test_import_project_happy_path_returns_manifest_and_configs(
    client, app_state, temp_topology_file
):
    fake = MagicMock()
    app_state.topology_backend = fake
    bundle = _valid_bundle(temp_topology_file.read_text())

    resp = client.post(
        "/project/import",
        files={"file": ("bundle.topoproj", bundle, "application/zip")},
    )
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]

    result = _wait_for_import_job(client, job_id)
    assert result["status"] == "done"
    fake.deploy_topology.assert_called_once()
    assert result["manifest"]["format_version"] == 1


def test_import_project_does_not_auto_push_by_default(
    client, app_state, temp_topology_file
):
    fake = MagicMock()
    app_state.topology_backend = fake
    bundle = _valid_bundle(
        temp_topology_file.read_text(), configs={"R1": ["show version"]}
    )

    with patch("app.config_push.push_all") as fake_push_all:
        resp = client.post(
            "/project/import",
            files={"file": ("bundle.topoproj", bundle, "application/zip")},
        )
        result = _wait_for_import_job(client, resp.json()["job_id"])

    assert result["status"] == "done"
    fake_push_all.assert_not_called()
    assert "push_results" not in result


def test_import_project_auto_pushes_when_the_bundle_enables_it(
    client, app_state, temp_topology_file
):
    """
    auto_config_push is a property of the .topoproj bundle itself
    (its manifest.json - see project_bundle.build_export), not a
    server-wide setting: a bundle someone else built and asked you to
    import shouldn't get to silently run commands on your live devices
    unless it says so itself, and you choose to import it at all.
    """
    fake = MagicMock()
    app_state.topology_backend = fake
    bundle = _valid_bundle(
        temp_topology_file.read_text(),
        configs={"R1": ["show version"]},
        auto_config_push=True,
    )
    fake_result = [DeviceResult(device="R1", transport="netmiko", ok=True)]

    with patch("app.config_push.push_all", return_value=fake_result) as fake_push_all:
        resp = client.post(
            "/project/import",
            files={"file": ("bundle.topoproj", bundle, "application/zip")},
        )
        result = _wait_for_import_job(client, resp.json()["job_id"])

    assert result["status"] == "done"
    assert fake_push_all.call_args[0][0] == {"R1": ["show version"]}
    assert result["push_results"] == [
        {
            "device": "R1",
            "transport": "netmiko",
            "ok": True,
            "commands": [],
            "error": None,
        }
    ]
    assert app_state.last_config_push == {"R1": ["show version"]}


def test_import_project_skips_auto_push_when_bundle_has_no_configs(
    client, app_state, temp_topology_file
):
    fake = MagicMock()
    app_state.topology_backend = fake
    bundle = _valid_bundle(temp_topology_file.read_text(), auto_config_push=True)

    with patch("app.config_push.push_all") as fake_push_all:
        resp = client.post(
            "/project/import",
            files={"file": ("bundle.topoproj", bundle, "application/zip")},
        )
        result = _wait_for_import_job(client, resp.json()["job_id"])

    assert result["status"] == "done"
    fake_push_all.assert_not_called()
    assert "push_results" not in result


def test_import_project_restores_bundled_node_positions(
    client, app_state, temp_topology_file, tmp_path, monkeypatch
):
    monkeypatch.setenv("TOPOLOGYOPERATOR_LAYOUT_FILE", str(tmp_path / "layout.json"))
    fake = MagicMock()
    app_state.topology_backend = fake
    bundle = _valid_bundle(
        temp_topology_file.read_text(),
        layout={"R1": {"x": 42, "y": 84}, "GHOST": {"x": 1, "y": 2}},
    )

    resp = client.post(
        "/project/import",
        files={"file": ("bundle.topoproj", bundle, "application/zip")},
    )
    assert resp.status_code == 200, resp.text
    result = _wait_for_import_job(client, resp.json()["job_id"])
    assert result["status"] == "done"

    layout = client.get("/layout").json()
    assert layout["R1"] == {"x": 42, "y": 84}
    assert "GHOST" not in layout


def test_import_project_bad_bundle_maps_to_error_status(
    client, app_state, temp_topology_file
):
    resp = client.post(
        "/project/import",
        files={"file": ("bundle.topoproj", b"not a zip", "application/zip")},
    )
    assert resp.status_code == 200, resp.text
    result = _wait_for_import_job(client, resp.json()["job_id"])
    assert result["status"] == "error"
    assert result["error_kind"] == "bad_bundle"


def test_import_project_deploy_failure_maps_to_error_status(
    client, app_state, temp_topology_file
):
    fake = MagicMock()
    fake.deploy_topology.side_effect = RuntimeError("deploy failed")
    app_state.topology_backend = fake
    bundle = _valid_bundle(temp_topology_file.read_text())

    resp = client.post(
        "/project/import",
        files={"file": ("bundle.topoproj", bundle, "application/zip")},
    )
    result = _wait_for_import_job(client, resp.json()["job_id"])
    assert result["status"] == "error"
    assert result["error_kind"] == "deploy_failed"


def test_import_job_status_404s_for_unknown_job(client, app_state):
    resp = client.get("/project/import/nonexistent-job-id")
    assert resp.status_code == 404


def test_settings_profiles_upload_list_and_delete_round_trip(
    client, app_state, tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR", str(tmp_path / "profiles")
    )

    resp = client.post(
        "/settings-profiles",
        files={
            "file": ("host-a.yml", b"esxi:\n  ip: 10.0.0.1\n", "application/x-yaml")
        },
    )
    assert resp.status_code == 200, resp.text

    profiles = client.get("/settings-profiles").json()
    assert len(profiles) == 1
    assert profiles[0]["filename"] == "host-a.yml"
    assert profiles[0]["esxi_ip"] == "10.0.0.1"

    resp = client.delete("/settings-profiles/host-a.yml")
    assert resp.status_code == 200, resp.text
    assert client.get("/settings-profiles").json() == []


def test_settings_profiles_upload_rejects_invalid_yaml(
    client, app_state, tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR", str(tmp_path / "profiles")
    )

    resp = client.post(
        "/settings-profiles",
        files={"file": ("host-a.yml", b"esxi: [unclosed", "application/x-yaml")},
    )
    assert resp.status_code == 400


def test_settings_profiles_delete_404s_for_unknown_file(
    client, app_state, tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR", str(tmp_path / "profiles")
    )
    resp = client.delete("/settings-profiles/nope.yml")
    assert resp.status_code == 404


def test_settings_profiles_activate_triggers_a_restart(
    client, app_state, tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR", str(tmp_path / "profiles")
    )
    client.post(
        "/settings-profiles",
        files={
            "file": ("host-a.yml", b"esxi:\n  ip: 10.0.0.1\n", "application/x-yaml")
        },
    )

    with patch("app.settings_profiles._restart_with_settings") as fake_restart:
        resp = client.post("/settings-profiles/host-a.yml/activate")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "restarting"
        time.sleep(1.0)

    fake_restart.assert_called_once()
    assert fake_restart.call_args[0][0].endswith("host-a.yml")


def test_settings_profiles_activate_404s_for_unknown_file(
    client, app_state, tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR", str(tmp_path / "profiles")
    )
    resp = client.post("/settings-profiles/nope.yml/activate")
    assert resp.status_code == 404


def test_generate_design_400s_for_empty_prompt(client, app_state):
    resp = client.post("/design/generate", json={"prompt": "   "})
    assert resp.status_code == 400


def test_generate_design_returns_job_id(client, app_state):
    from app import generate_jobs

    fake_job = generate_jobs.GenerateJob(id="job-1", max_attempts=4)
    with patch("app.main.generate_jobs.start_job", return_value=fake_job):
        resp = client.post(
            "/design/generate", json={"prompt": "a small office network"}
        )
    assert resp.status_code == 200
    assert resp.json() == {"job_id": "job-1"}


def test_generate_design_status_404s_for_unknown_job(client, app_state):
    resp = client.get("/design/generate/nope")
    assert resp.status_code == 404


def test_generate_design_status_reports_running_attempt(client, app_state):
    from app import generate_jobs

    app_state.generate_jobs = {
        "job-1": generate_jobs.GenerateJob(
            id="job-1", status="running", attempt=2, max_attempts=4, warnings=["hmm"]
        )
    }
    resp = client.get("/design/generate/job-1")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "running",
        "attempt": 2,
        "max_attempts": 4,
        "warnings": ["hmm"],
    }


def test_topology_503s_when_graph_failed_to_load(client, app_state):
    app_state.graph = None
    app_state.startup_warning = "template service unreachable"
    resp = client.get("/topology")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "template service unreachable"


def test_nodes_503s_when_graph_failed_to_load(client, app_state):
    app_state.graph = None
    resp = client.get("/nodes")
    assert resp.status_code == 503


def test_layout_get_and_post_503_when_graph_failed_to_load(client, app_state):
    app_state.graph = None
    assert client.get("/layout").status_code == 503
    assert (
        client.post("/layout", json={"name": "R1", "x": 1.0, "y": 2.0}).status_code
        == 503
    )


def test_design_endpoints_still_work_when_graph_failed_to_load(client, app_state):
    """The whole point of degrading gracefully: design mode never touches app.state.graph at all."""
    app_state.graph = None
    app_state.startup_warning = "template service unreachable"

    resp = client.post(
        "/design/nodes", json={"name": "R1", "role": "ROUTER", "image": "VPCS"}
    )
    assert resp.status_code == 200, resp.text
    assert client.get("/design/topology").status_code == 200


def test_reload_endpoint_recovers_after_a_transient_failure(client, app_state):
    app_state.graph = None
    app_state.startup_warning = "boom"

    with (
        patch("app.main.load_graph") as fake_load_graph,
        patch("app.main.resolve_project_id", return_value=None),
        patch("app.main.gns3_vm_deploy_jobs.auto_deploy_if_missing"),
    ):
        fake_load_graph.return_value = MagicMock(nodes={})
        resp = client.post("/reload")

    assert resp.status_code == 200
    assert resp.json()["warning"] is None or "GNS3" in resp.json()["warning"]
    assert app_state.graph is not None


def test_live_links_returns_empty_list_with_no_live_project(client, app_state):
    app_state.project_id = None
    resp = client.get("/links")
    assert resp.status_code == 200
    assert resp.json() == []


def test_link_capture_start_stop_and_stream(client, app_state):
    app_state.project_id = "proj-1"
    app_state.gns3_vm_ip = "10.20.20.233"
    live_nodes = [{"name": "R1", "node_id": "n1"}, {"name": "SW1", "node_id": "n2"}]
    live_links = [
        {
            "link_id": "link-1",
            "capturing": False,
            "nodes": [
                {"node_id": "n1", "label": {"text": "eth0"}},
                {"node_id": "n2", "label": {"text": "Ethernet0"}},
            ],
        }
    ]
    with (
        patch(
            "app.gns3_state.GNS3Connection.list_project_nodes", return_value=live_nodes
        ),
        patch("app.capture.GNS3Connection.list_project_links", return_value=live_links),
        patch("app.capture.GNS3Connection.ensure_project_open"),
    ):
        resp = client.get("/links")
        assert resp.status_code == 200
        assert resp.json() == [
            {
                "node1": "R1",
                "if1": "eth0",
                "node2": "SW1",
                "if2": "Ethernet0",
                "capturing": False,
                "delay_ms": 0,
                "jitter_ms": 0,
                "loss_percent": 0,
                "corrupt_percent": 0,
                "suspended": False,
                "other_filters": [],
            }
        ]

        with patch("app.capture.GNS3Connection.post", return_value={"capturing": True}):
            resp = client.post("/links/R1/SW1/capture/start")
        assert resp.status_code == 200, resp.text

        with patch("app.capture.GNS3Connection.post"):
            resp = client.post("/links/R1/SW1/capture/stop")
        assert resp.status_code == 200, resp.text

        fake_stream = MagicMock()
        fake_stream.iter_content.return_value = iter([b"pcap-bytes"])
        with patch("app.capture.requests.get", return_value=fake_stream):
            resp = client.get("/links/R1/SW1/capture/stream")
        assert resp.status_code == 200
        assert resp.content == b"pcap-bytes"
        assert resp.headers["content-type"] == "application/vnd.tcpdump.pcap"


def test_link_capture_start_404s_when_no_live_link(client, app_state):
    app_state.project_id = "proj-1"
    with patch("app.gns3_state.GNS3Connection.list_project_nodes", return_value=[]):
        resp = client.post("/links/R1/SW1/capture/start")
    assert resp.status_code == 404


def test_generate_design_status_reports_error(client, app_state):
    from app import generate_jobs

    app_state.generate_jobs = {
        "job-1": generate_jobs.GenerateJob(
            id="job-1",
            status="error",
            attempt=4,
            max_attempts=4,
            error="no valid topology",
        )
    }
    resp = client.get("/design/generate/job-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert body["error"] == "no valid topology"


def test_health_offers_the_selected_project_when_gns3_has_none(
    client, app_state, monkeypatch, tmp_path
):
    monkeypatch.setenv("TOPOLOGYOPERATOR_SETTINGS", str(tmp_path / "host.yml"))
    (tmp_path / "host.yml").write_text("esxi:\n  ip: 192.0.2.1\n")
    app_state.project_name = "Office"
    app_state.project_id = None
    app_state.gns3_vm_ip = "10.0.0.5"
    selection = {"project_id": "abc", "project_name": "Office"}
    with patch("app.main.project_selection.get_selection", return_value=selection):
        offered = client.get("/health").json()["deployable_project"]
        assert offered == {"id": "abc", "name": "Office", "filename": "host.yml"}

        app_state.project_id = "gns3-uuid"  # already deployed: nothing to offer
        assert client.get("/health").json()["deployable_project"] is None

        app_state.project_id = None
        app_state.gns3_vm_ip = None  # GNS3 VM unreachable: deploying can't work
        assert client.get("/health").json()["deployable_project"] is None


def test_health_offers_nothing_without_a_selected_project(
    client, app_state, monkeypatch, tmp_path
):
    monkeypatch.setenv("TOPOLOGYOPERATOR_SETTINGS", str(tmp_path / "host.yml"))
    (tmp_path / "host.yml").write_text("esxi:\n  ip: 192.0.2.1\n")
    app_state.project_name = "Office"
    app_state.project_id = None
    app_state.gns3_vm_ip = "10.0.0.5"
    selection = {"project_id": None, "project_name": None}
    with patch("app.main.project_selection.get_selection", return_value=selection):
        assert client.get("/health").json()["deployable_project"] is None
