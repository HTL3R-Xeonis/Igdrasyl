import io
import json
import time
import zipfile
from unittest.mock import MagicMock, patch

from app import import_jobs
from app.config_push import DeviceResult


def _bundle_with_topology(topology_yaml_text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("topology.yaml", topology_yaml_text)
    return buf.getvalue()


def test_peek_target_node_names_extracts_every_group(temp_topology_file):
    bundle = _bundle_with_topology(temp_topology_file.read_text())
    assert import_jobs.peek_target_node_names(bundle) == {"R1", "SW1", "VM1"}


def test_peek_target_node_names_returns_empty_set_for_malformed_bundle():
    assert import_jobs.peek_target_node_names(b"not a zip") == set()


def test_peek_target_node_names_returns_empty_set_when_topology_yaml_missing():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({}))
    assert import_jobs.peek_target_node_names(buf.getvalue()) == set()


def _fake_app(app_state):
    class FakeApp:
        state = app_state

    return FakeApp()


def test_create_job_sets_total_from_target_names(app_state):
    job = import_jobs.create_job(_fake_app(app_state), {"R1", "SW1"})
    assert job.total == 2
    assert job.status == "running"
    assert job.completed == 0


def test_poll_progress_counts_live_esxi_vms_by_name(app_state):
    app = _fake_app(app_state)
    job = import_jobs.create_job(app, {"VM1", "VM2"})
    app_state.esxi_connection.get_vm.side_effect = lambda name: (
        object() if name == "VM1" else None
    )

    completed = import_jobs.poll_progress(app, job)

    assert completed == 1
    assert job.completed == 1


def test_poll_progress_counts_live_gns3_nodes_by_name(app_state):
    app = _fake_app(app_state)
    app_state.gns3_vm_ip = "10.20.20.231"
    app_state.project_name = "proj"
    app_state.esxi_connection.get_vm.return_value = None
    job = import_jobs.create_job(app, {"R1", "SW1"})

    live = [{"name": "R1"}]
    with (
        patch("app.import_jobs.resolve_project_id", return_value="pid-1"),
        patch("app.import_jobs.GNS3Connection.list_project_nodes", return_value=live),
    ):
        completed = import_jobs.poll_progress(app, job)

    assert completed == 1


def test_poll_progress_never_exceeds_total(app_state):
    app = _fake_app(app_state)
    app_state.esxi_connection.get_vm.return_value = object()
    job = import_jobs.create_job(app, {"VM1"})

    completed = import_jobs.poll_progress(app, job)

    assert completed == 1
    assert completed <= job.total


def test_poll_progress_is_a_noop_once_job_is_no_longer_running(app_state):
    app = _fake_app(app_state)
    job = import_jobs.create_job(app, {"VM1"})
    job.status = "done"
    job.completed = 1

    assert import_jobs.poll_progress(app, job) == 1
    app_state.esxi_connection.get_vm.assert_not_called()


def test_run_import_job_marks_done_and_stores_result(app_state, temp_topology_file):
    app = _fake_app(app_state)
    app_state.topology_backend = MagicMock()
    job = import_jobs.create_job(app, set())

    fake_result = {
        "manifest": {},
        "configs": {},
        "drawings": [],
        "auto_config_push": False,
    }
    with patch("app.project_bundle.apply_import", return_value=fake_result):
        import_jobs.run_import_job(app, job, b"irrelevant")

    assert job.status == "done"
    assert job.completed == job.total
    assert job.result == fake_result


def test_run_import_job_marks_error_on_bad_bundle(app_state):
    app = _fake_app(app_state)
    job = import_jobs.create_job(app, set())

    with patch("app.project_bundle.apply_import", side_effect=ValueError("bad bundle")):
        import_jobs.run_import_job(app, job, b"irrelevant")

    assert job.status == "error"
    assert job.error_kind == "bad_bundle"
    assert job.error == "bad bundle"


def test_run_import_job_marks_error_on_deploy_failure(app_state):
    app = _fake_app(app_state)
    job = import_jobs.create_job(app, set())

    with patch(
        "app.project_bundle.apply_import", side_effect=RuntimeError("deploy failed")
    ):
        import_jobs.run_import_job(app, job, b"irrelevant")

    assert job.status == "error"
    assert job.error_kind == "deploy_failed"


def test_run_import_job_auto_pushes_when_result_says_so(app_state):
    app = _fake_app(app_state)
    job = import_jobs.create_job(app, set())

    fake_result = {
        "manifest": {},
        "configs": {"R1": ["show version"]},
        "drawings": [],
        "auto_config_push": True,
    }
    with (
        patch("app.project_bundle.apply_import", return_value=fake_result),
        patch(
            "app.config_push.push_all",
            return_value=[DeviceResult(device="R1", transport="netmiko", ok=True)],
        ) as fake_push_all,
    ):
        import_jobs.run_import_job(app, job, b"irrelevant")

    fake_push_all.assert_called_once_with({"R1": ["show version"]}, app, None)
    assert [r.device for r in job.result["push_results"]] == ["R1"]
    assert app_state.last_config_push == {"R1": ["show version"]}


def test_start_job_runs_in_the_background_and_completes(app_state, temp_topology_file):
    app = _fake_app(app_state)
    app_state.topology_backend = MagicMock()
    bundle = _bundle_with_topology(temp_topology_file.read_text())

    fake_result = {
        "manifest": {},
        "configs": {},
        "drawings": [],
        "auto_config_push": False,
    }
    with patch("app.project_bundle.apply_import", return_value=fake_result):
        job = import_jobs.start_job(app, bundle)

        deadline = time.monotonic() + 2
        while job.status == "running" and time.monotonic() < deadline:
            time.sleep(0.01)

    assert job.status == "done"
    assert job.total == 3  # R1, SW1, VM1 from the fixture topology


_MISSING = "Image 'NAT' not found on ESXi or GNS3"


def test_run_import_job_repeats_the_import_once_the_template_service_recovered(
    app_state,
):
    app = _fake_app(app_state)
    job = import_jobs.create_job(app, set())
    fake_result = {
        "manifest": {},
        "configs": {},
        "drawings": [],
        "auto_config_push": False,
    }

    with (
        patch(
            "app.project_bundle.apply_import",
            side_effect=[ValueError(_MISSING), fake_result],
        ) as apply,
        patch("app.template_health.heal", return_value=True),
    ):
        import_jobs.run_import_job(app, job, b"irrelevant")

    assert apply.call_count == 2
    assert job.status == "done"


def test_run_import_job_reports_a_really_missing_image_without_a_second_try(app_state):
    app = _fake_app(app_state)
    job = import_jobs.create_job(app, set())

    with (
        patch(
            "app.project_bundle.apply_import", side_effect=ValueError(_MISSING)
        ) as apply,
        patch("app.template_health.heal", return_value=False),
        patch("app.template_health.explain", side_effect=lambda text: text),
    ):
        import_jobs.run_import_job(app, job, b"irrelevant")

    assert apply.call_count == 1
    assert job.status == "error" and job.error_kind == "bad_bundle"


def test_run_import_job_does_not_try_to_heal_other_bundle_errors(app_state):
    app = _fake_app(app_state)
    job = import_jobs.create_job(app, set())

    with (
        patch("app.project_bundle.apply_import", side_effect=ValueError("bad bundle")),
        patch("app.template_health.heal") as heal,
    ):
        import_jobs.run_import_job(app, job, b"irrelevant")

    heal.assert_not_called()


def test_poll_progress_names_the_devices_still_to_create(app_state):
    app = _fake_app(app_state)
    job = import_jobs.create_job(app, {"VM1", "VM2", "VM3"})
    app_state.esxi_connection.get_vm.side_effect = lambda name: (
        object() if name == "VM1" else None
    )

    import_jobs.poll_progress(app, job)

    assert job.pending == ["VM2", "VM3"]
    assert job.phase == "creating devices"


def test_phase_says_finishing_once_every_device_exists(app_state):
    app = _fake_app(app_state)
    job = import_jobs.create_job(app, {"VM1"})
    app_state.esxi_connection.get_vm.side_effect = lambda name: object()

    import_jobs.poll_progress(app, job)

    assert job.pending == []
    assert job.phase.startswith("finishing")


def test_phase_is_empty_once_the_job_is_over(app_state):
    job = import_jobs.create_job(_fake_app(app_state), {"VM1"})
    job.status = "done"

    assert job.phase == ""


def test_elapsed_seconds_counts_from_the_start(app_state):
    job = import_jobs.create_job(_fake_app(app_state), {"VM1"})
    job.started_at -= 90

    assert 90 <= job.elapsed_seconds < 95
