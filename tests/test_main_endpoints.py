import io
import json
import time
import zipfile
from unittest.mock import MagicMock, patch

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
    with patch("app.gns3_state.GNS3Connection.list_project_nodes", return_value=live), patch(
        "app.gns3_state.GNS3Connection.list_project_links", return_value=[]
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


def test_layout_get_returns_a_position_for_every_node(client, app_state, tmp_path, monkeypatch):
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
    drawing = {"id": 1, "type": "rect", "x1": 0, "y1": 0, "x2": 100, "y2": 50, "stroke": "#7cb342"}
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
        "manifest.json", "topology.yaml", "settings.yml", "configs.json", "drawings.json", "layout.json",
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


def test_import_project_does_not_auto_push_by_default(client, app_state, temp_topology_file):
    fake = MagicMock()
    app_state.topology_backend = fake
    bundle = _valid_bundle(temp_topology_file.read_text(), configs={"R1": ["show version"]})

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
    assert result["push_results"] == [{"device": "R1", "transport": "netmiko", "ok": True, "commands": [], "error": None}]
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


def test_import_project_bad_bundle_maps_to_error_status(client, app_state, temp_topology_file):
    resp = client.post(
        "/project/import",
        files={"file": ("bundle.topoproj", b"not a zip", "application/zip")},
    )
    assert resp.status_code == 200, resp.text
    result = _wait_for_import_job(client, resp.json()["job_id"])
    assert result["status"] == "error"
    assert result["error_kind"] == "bad_bundle"


def test_import_project_deploy_failure_maps_to_error_status(client, app_state, temp_topology_file):
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
