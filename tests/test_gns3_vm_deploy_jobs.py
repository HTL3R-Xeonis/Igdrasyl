import time
from unittest.mock import MagicMock, patch

import pytest
from src.settings import Settings

from app import gns3_vm_deploy_jobs, ova_templates


def _fake_app(app_state):
    class FakeApp:
        state = app_state

    return FakeApp()


@pytest.fixture(autouse=True)
def _reset_gns3_vm_settings(monkeypatch):
    original = (
        Settings.ESXI.GNS3_VM_OVA_PATH,
        Settings.ESXI.GNS3_VM_MGMT_NETWORK,
        Settings.ESXI.DATASTORE,
        Settings.ESXI.GNS3_VM_MAC_RANGE,
    )
    ova_templates._selected_template_id = None

    monkeypatch.setattr(ova_templates, "filename_for", lambda template_id: template_id)
    yield
    (
        Settings.ESXI.GNS3_VM_OVA_PATH,
        Settings.ESXI.GNS3_VM_MGMT_NETWORK,
        Settings.ESXI.DATASTORE,
        Settings.ESXI.GNS3_VM_MAC_RANGE,
    ) = original
    ova_templates._selected_template_id = None


def test_start_job_raises_not_configured_without_ova_path(app_state):
    Settings.ESXI.GNS3_VM_OVA_PATH = None
    Settings.ESXI.GNS3_VM_MGMT_NETWORK = "PG-MGMT"

    with pytest.raises(gns3_vm_deploy_jobs.NotConfigured):
        gns3_vm_deploy_jobs.start_job(_fake_app(app_state))


def test_start_job_raises_not_configured_without_mgmt_network(app_state):
    Settings.ESXI.GNS3_VM_OVA_PATH = "/mnt/nfs/gns3.ova"
    Settings.ESXI.GNS3_VM_MGMT_NETWORK = None

    with pytest.raises(gns3_vm_deploy_jobs.NotConfigured):
        gns3_vm_deploy_jobs.start_job(_fake_app(app_state))


def test_start_job_raises_already_running_for_a_second_call(app_state):
    # A running job is set up directly (rather than via a real start_job
    # + background thread) to avoid a race against patch() teardown: the
    # patched deploy_fresh_gns3_vm is only guaranteed mocked for as long
    # as the `with` block below is open, but the background thread it
    # dispatches keeps running past that.
    Settings.ESXI.GNS3_VM_OVA_PATH = "/mnt/nfs/gns3.ova"
    Settings.ESXI.GNS3_VM_MGMT_NETWORK = "PG-MGMT"
    app = _fake_app(app_state)
    app_state.gns3_vm_deploy_job = gns3_vm_deploy_jobs.GNS3VMDeployJob(
        id="already-running", status="running"
    )

    with pytest.raises(gns3_vm_deploy_jobs.AlreadyRunning):
        gns3_vm_deploy_jobs.start_job(app)


def test_run_deploy_job_marks_done_and_updates_app_state(app_state):
    Settings.ESXI.GNS3_VM_OVA_PATH = "/mnt/nfs/gns3.ova"
    Settings.ESXI.GNS3_VM_MGMT_NETWORK = "PG-MGMT"
    Settings.ESXI.DATASTORE = "datastore1"
    app = _fake_app(app_state)
    job = gns3_vm_deploy_jobs.GNS3VMDeployJob(id="job-1")

    with (
        patch(
            "app.gns3_vm_deploy_jobs.deploy_fresh_gns3_vm", return_value="10.20.20.233"
        ) as fake_deploy,
        patch("app.gns3_vm_deploy_jobs.resolve_project_id", return_value=None),
    ):
        gns3_vm_deploy_jobs.run_deploy_job(app, job)

    assert job.status == "done"
    assert job.ip_address == "10.20.20.233"
    assert app_state.gns3_vm_ip == "10.20.20.233"
    fake_deploy.assert_called_once()
    args, kwargs = fake_deploy.call_args
    assert kwargs["ova_filename"] == "/mnt/nfs/gns3.ova"
    assert args[3] == "datastore1"
    assert args[4] == "PG-MGMT"
    assert "on_stage" in kwargs
    assert kwargs["delete_old_vm"] is False


def test_run_deploy_job_forwards_delete_old_vm(app_state):
    Settings.ESXI.GNS3_VM_OVA_PATH = "/mnt/nfs/gns3.ova"
    Settings.ESXI.GNS3_VM_MGMT_NETWORK = "PG-MGMT"
    Settings.ESXI.DATASTORE = "datastore1"
    app = _fake_app(app_state)
    job = gns3_vm_deploy_jobs.GNS3VMDeployJob(id="job-1")

    with (
        patch(
            "app.gns3_vm_deploy_jobs.deploy_fresh_gns3_vm", return_value="10.20.20.233"
        ) as fake_deploy,
        patch("app.gns3_vm_deploy_jobs.resolve_project_id", return_value=None),
    ):
        gns3_vm_deploy_jobs.run_deploy_job(app, job, delete_old_vm=True)

    assert fake_deploy.call_args.kwargs["delete_old_vm"] is True


def test_run_deploy_job_forwards_mac_range_from_settings(app_state):
    Settings.ESXI.GNS3_VM_OVA_PATH = "/mnt/nfs/gns3.ova"
    Settings.ESXI.GNS3_VM_MGMT_NETWORK = "PG-MGMT"
    Settings.ESXI.DATASTORE = "datastore1"
    Settings.ESXI.GNS3_VM_MAC_RANGE = ("00:50:56:00:10:00", "00:50:56:00:10:ff")
    app = _fake_app(app_state)
    job = gns3_vm_deploy_jobs.GNS3VMDeployJob(id="job-1")

    with (
        patch(
            "app.gns3_vm_deploy_jobs.deploy_fresh_gns3_vm", return_value="10.20.20.233"
        ) as fake_deploy,
        patch("app.gns3_vm_deploy_jobs.resolve_project_id", return_value=None),
    ):
        gns3_vm_deploy_jobs.run_deploy_job(app, job)

    assert fake_deploy.call_args.kwargs["mac_range"] == (
        "00:50:56:00:10:00",
        "00:50:56:00:10:ff",
    )


def test_start_job_forwards_delete_old_vm_to_run_deploy_job(app_state):
    Settings.ESXI.GNS3_VM_OVA_PATH = "/mnt/nfs/gns3.ova"
    Settings.ESXI.GNS3_VM_MGMT_NETWORK = "PG-MGMT"
    Settings.ESXI.DATASTORE = "datastore1"
    app = _fake_app(app_state)

    with (
        patch(
            "app.gns3_vm_deploy_jobs.deploy_fresh_gns3_vm", return_value="10.20.20.233"
        ) as fake_deploy,
        patch("app.gns3_vm_deploy_jobs.resolve_project_id", return_value=None),
    ):
        job = gns3_vm_deploy_jobs.start_job(app, delete_old_vm=True)
        deadline = time.monotonic() + 2
        while job.status == "running" and time.monotonic() < deadline:
            time.sleep(0.01)

    assert job.status == "done"
    assert fake_deploy.call_args.kwargs["delete_old_vm"] is True


def test_run_deploy_job_auto_picks_biggest_datastore_when_unset(app_state):
    Settings.ESXI.GNS3_VM_OVA_PATH = "/mnt/nfs/gns3.ova"
    Settings.ESXI.GNS3_VM_MGMT_NETWORK = "PG-MGMT"
    Settings.ESXI.DATASTORE = None
    app = _fake_app(app_state)
    job = gns3_vm_deploy_jobs.GNS3VMDeployJob(id="job-1")
    biggest = MagicMock()
    biggest.name = "auto-picked-datastore"
    app_state.esxi_connection.find_biggest_datastore.return_value = biggest

    with (
        patch(
            "app.gns3_vm_deploy_jobs.deploy_fresh_gns3_vm", return_value="10.20.20.233"
        ) as fake_deploy,
        patch("app.gns3_vm_deploy_jobs.resolve_project_id", return_value=None),
    ):
        gns3_vm_deploy_jobs.run_deploy_job(app, job)

    args, _ = fake_deploy.call_args
    assert args[3] == "auto-picked-datastore"


def test_run_deploy_job_marks_error_on_failure(app_state):
    Settings.ESXI.GNS3_VM_OVA_PATH = "/mnt/nfs/gns3.ova"
    Settings.ESXI.GNS3_VM_MGMT_NETWORK = "PG-MGMT"
    Settings.ESXI.DATASTORE = "datastore1"
    app = _fake_app(app_state)
    job = gns3_vm_deploy_jobs.GNS3VMDeployJob(id="job-1")

    with patch(
        "app.gns3_vm_deploy_jobs.deploy_fresh_gns3_vm",
        side_effect=TimeoutError("no IP reported"),
    ):
        gns3_vm_deploy_jobs.run_deploy_job(app, job)

    assert job.status == "error"
    assert job.error == "no IP reported"


def test_run_deploy_job_reports_stages_via_on_stage(app_state):
    Settings.ESXI.GNS3_VM_OVA_PATH = "/mnt/nfs/gns3.ova"
    Settings.ESXI.GNS3_VM_MGMT_NETWORK = "PG-MGMT"
    Settings.ESXI.DATASTORE = "datastore1"
    app = _fake_app(app_state)
    job = gns3_vm_deploy_jobs.GNS3VMDeployJob(id="job-1")

    def fake_deploy(*args, **kwargs):
        kwargs["on_stage"]("importing OVA")
        return "10.20.20.233"

    with (
        patch("app.gns3_vm_deploy_jobs.deploy_fresh_gns3_vm", side_effect=fake_deploy),
        patch("app.gns3_vm_deploy_jobs.resolve_project_id", return_value=None),
    ):
        gns3_vm_deploy_jobs.run_deploy_job(app, job)

    assert job.stage == "importing OVA"


def test_start_job_runs_in_the_background_and_completes(app_state):
    Settings.ESXI.GNS3_VM_OVA_PATH = "/mnt/nfs/gns3.ova"
    Settings.ESXI.GNS3_VM_MGMT_NETWORK = "PG-MGMT"
    Settings.ESXI.DATASTORE = "datastore1"
    app = _fake_app(app_state)

    with (
        patch(
            "app.gns3_vm_deploy_jobs.deploy_fresh_gns3_vm", return_value="10.20.20.233"
        ),
        patch("app.gns3_vm_deploy_jobs.resolve_project_id", return_value=None),
    ):
        job = gns3_vm_deploy_jobs.start_job(app)

        deadline = time.monotonic() + 2
        while job.status == "running" and time.monotonic() < deadline:
            time.sleep(0.01)

    assert job.status == "done"
    assert job.ip_address == "10.20.20.233"


def test_auto_deploy_if_missing_is_a_noop_when_unconfigured(app_state):
    Settings.ESXI.GNS3_VM_OVA_PATH = None
    Settings.ESXI.GNS3_VM_MGMT_NETWORK = None
    app = _fake_app(app_state)
    app_state.esxi_connection.get_vm.return_value = None

    with patch("app.gns3_vm_deploy_jobs.deploy_fresh_gns3_vm") as fake_deploy:
        gns3_vm_deploy_jobs.auto_deploy_if_missing(app)

    fake_deploy.assert_not_called()


def test_auto_deploy_if_missing_is_a_noop_when_vm_already_exists(app_state):
    Settings.ESXI.GNS3_VM_OVA_PATH = "/mnt/nfs/gns3.ova"
    Settings.ESXI.GNS3_VM_MGMT_NETWORK = "PG-MGMT"
    app = _fake_app(app_state)
    app_state.esxi_connection.get_vm.return_value = (
        MagicMock()
    )  # exists, even with no IP yet

    with patch("app.gns3_vm_deploy_jobs.deploy_fresh_gns3_vm") as fake_deploy:
        gns3_vm_deploy_jobs.auto_deploy_if_missing(app)

    fake_deploy.assert_not_called()
    app_state.esxi_connection.power_off_vm.assert_not_called()


def test_auto_deploy_if_missing_deploys_when_configured_and_truly_absent(app_state):
    Settings.ESXI.GNS3_VM_OVA_PATH = "/mnt/nfs/gns3.ova"
    Settings.ESXI.GNS3_VM_MGMT_NETWORK = "PG-MGMT"
    Settings.ESXI.DATASTORE = "datastore1"
    app = _fake_app(app_state)
    app_state.esxi_connection.get_vm.return_value = None

    with patch(
        "app.gns3_vm_deploy_jobs.deploy_fresh_gns3_vm", return_value="10.20.20.233"
    ) as fake_deploy:
        gns3_vm_deploy_jobs.auto_deploy_if_missing(app)

    fake_deploy.assert_called_once()
    args, kwargs = fake_deploy.call_args
    assert args[1] == Settings.ESXI.GNS3_VM_NAME
    assert kwargs["ova_filename"] == "/mnt/nfs/gns3.ova"
    assert args[3] == "datastore1"
    assert args[4] == "PG-MGMT"
    assert args[5] == Settings.ESXI.TRUNK_PORT_GROUP


def test_auto_deploy_if_missing_forwards_mac_range_from_settings(app_state):
    Settings.ESXI.GNS3_VM_OVA_PATH = "/mnt/nfs/gns3.ova"
    Settings.ESXI.GNS3_VM_MGMT_NETWORK = "PG-MGMT"
    Settings.ESXI.DATASTORE = "datastore1"
    Settings.ESXI.GNS3_VM_MAC_RANGE = ("00:50:56:00:10:00", "00:50:56:00:10:ff")
    app = _fake_app(app_state)
    app_state.esxi_connection.get_vm.return_value = None

    with patch(
        "app.gns3_vm_deploy_jobs.deploy_fresh_gns3_vm", return_value="10.20.20.233"
    ) as fake_deploy:
        gns3_vm_deploy_jobs.auto_deploy_if_missing(app)

    assert fake_deploy.call_args.kwargs["mac_range"] == (
        "00:50:56:00:10:00",
        "00:50:56:00:10:ff",
    )


def test_auto_deploy_if_missing_propagates_deploy_failures(app_state):
    Settings.ESXI.GNS3_VM_OVA_PATH = "/mnt/nfs/gns3.ova"
    Settings.ESXI.GNS3_VM_MGMT_NETWORK = "PG-MGMT"
    Settings.ESXI.DATASTORE = "datastore1"
    app = _fake_app(app_state)
    app_state.esxi_connection.get_vm.return_value = None

    with (
        patch(
            "app.gns3_vm_deploy_jobs.deploy_fresh_gns3_vm",
            side_effect=TimeoutError("no IP reported"),
        ),
        pytest.raises(TimeoutError),
    ):
        gns3_vm_deploy_jobs.auto_deploy_if_missing(app)


def test_an_unexpected_error_ends_the_job_instead_of_leaving_it_running(app_state):
    from unittest.mock import patch

    from app import gns3_vm_deploy_jobs

    app = app_state
    job = gns3_vm_deploy_jobs.GNS3VMDeployJob(id="j")
    with (
        patch(
            "app.gns3_vm_deploy_jobs.ova_templates.selected_template_id",
            return_value="tpl",
        ),
        patch(
            "app.gns3_vm_deploy_jobs.ova_templates.filename_for",
            side_effect=OSError(28, "No space left on device"),
        ),
        patch.object(gns3_vm_deploy_jobs.Settings.ESXI, "DATASTORE", "ds", create=True),
    ):
        gns3_vm_deploy_jobs.run_deploy_job(app, job)
    assert job.status == "error"
    assert "No space left" in job.error
