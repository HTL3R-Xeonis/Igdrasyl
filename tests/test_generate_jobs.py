"""
Tests app/generate_jobs.py. LITERAL_API_VALUES is forced "true" for the
whole suite (see conftest.py's own top-of-file comment), so
TopologyFileValidation's own template check and Environment.
get_environment both resolve against the same literal template sets
(Settings.API.LITERAL_GNS3_TEMPLATES/LITERAL_ESXI_TEMPLATES) for real,
with nothing mocked - "Cisco IOSv 15.6(1)T"/"VPCS"/"debian" below are
real, valid images under that literal set, not fixtures of convenience.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import yaml

from app import generate_jobs


def _fake_app(app_state):
    class FakeApp:
        state = app_state

    return FakeApp()


_VALID_YAML = yaml.safe_dump(
    {
        "nodes": [
            {"names": ["R1"], "role": "ROUTER", "image": "Cisco IOSv 15.6(1)T"},
            {"names": ["PC1"], "role": "PC", "image": "VPCS"},
        ],
        "edges": [["R1", "Gi0/0", "PC1", "Ethernet0"]],
    }
)


def _valid_result(warnings=None):
    return {"valid": True, "yaml": _VALID_YAML, "warnings": warnings or []}


def test_load_generated_topology_builds_design_state():
    nodes = [
        {"names": ["R1", "R2"], "role": "ROUTER", "image": "Cisco IOSv 15.6(1)T"},
        {"names": ["PC1"], "role": "PC", "image": "VPCS"},
    ]
    edges = [["R1", "Gi0/0", "PC1", "Ethernet0"]]

    state = generate_jobs._load_generated_topology(nodes, edges)

    assert set(state.nodes) == {"R1", "R2", "PC1"}
    assert state.nodes["R1"].environment == "ON_GNS3"
    assert state.links == [("R1", "Gi0/0", "PC1", "Ethernet0")]


def test_load_generated_topology_gives_every_node_a_distinct_position():
    # Real bug: without this, every generated node fell back to the
    # frontend's own (100, 100) default (nothing placed them, unlike
    # the click-to-place flow every other node creation path uses) and
    # rendered as one fully overlapping stack of icons.
    nodes = [
        {
            "names": ["R1", "R2", "R3", "R4"],
            "role": "ROUTER",
            "image": "Cisco IOSv 15.6(1)T",
        },
    ]
    state = generate_jobs._load_generated_topology(nodes, [])

    assert set(state.layout) == {"R1", "R2", "R3", "R4"}
    positions = [(p["x"], p["y"]) for p in state.layout.values()]
    assert len(set(positions)) == 4


def test_run_generate_job_succeeds_on_first_valid_attempt(app_state):
    app = _fake_app(app_state)
    job = generate_jobs.GenerateJob(id="job-1", max_attempts=3)

    with patch(
        "app.generate_jobs.TopologyGeneratorClient.generate_topology",
        return_value=_valid_result(warnings=["heads up"]),
    ) as fake_generate:
        generate_jobs.run_generate_job(app, job, "a small office network")

    assert job.status == "done"
    assert job.attempt == 1
    assert job.warnings == ["heads up"]
    assert set(app_state.design.nodes) == {"R1", "PC1"}
    fake_generate.assert_called_once_with("a small office network")


def test_run_generate_job_retries_when_api_reports_invalid(app_state):
    app = _fake_app(app_state)
    job = generate_jobs.GenerateJob(id="job-1", max_attempts=3)
    responses = [{"valid": False, "warnings": ["bad prompt"]}, _valid_result()]

    with patch(
        "app.generate_jobs.TopologyGeneratorClient.generate_topology",
        side_effect=responses,
    ):
        generate_jobs.run_generate_job(app, job, "prompt")

    assert job.status == "done"
    assert job.attempt == 2


def test_run_generate_job_retries_when_local_validation_fails(app_state):
    app = _fake_app(app_state)
    job = generate_jobs.GenerateJob(id="job-1", max_attempts=3)
    bad_yaml = yaml.safe_dump(
        {
            "nodes": [{"names": ["R1"], "role": "ROUTER", "image": "not-a-real-image"}],
            "edges": [],
        }
    )
    responses = [
        {"valid": True, "yaml": bad_yaml, "warnings": []},
        _valid_result(),
    ]

    with patch(
        "app.generate_jobs.TopologyGeneratorClient.generate_topology",
        side_effect=responses,
    ):
        generate_jobs.run_generate_job(app, job, "prompt")

    assert job.status == "done"
    assert job.attempt == 2


def test_run_generate_job_fails_after_max_attempts(app_state):
    app = _fake_app(app_state)
    job = generate_jobs.GenerateJob(id="job-1", max_attempts=2)
    original_design = app_state.design

    with patch(
        "app.generate_jobs.TopologyGeneratorClient.generate_topology",
        return_value={"valid": False, "warnings": []},
    ) as fake_generate:
        generate_jobs.run_generate_job(app, job, "prompt")

    assert job.status == "error"
    assert job.attempt == 2
    assert "2 attempt" in job.error
    assert fake_generate.call_count == 2
    assert app_state.design is original_design


def test_run_generate_job_error_names_the_last_validation_problem(app_state):
    # The real failure seen live: the generator's own check passed, but
    # TopologyFileValidation rejected the LLM's reuse of an interface name.
    app = _fake_app(app_state)
    job = generate_jobs.GenerateJob(id="job-1", max_attempts=2)
    duplicate_interface_yaml = (
        "nodes:\n"
        "  - image: VPCS\n    role: PC\n    names: [PC1, PC2]\n"
        "  - image: Ethernet switch\n    role: SWITCH\n    names: [SW1]\n"
        "edges:\n"
        "  - [PC1, eth0, SW1, gi0/1]\n"
        "  - [PC2, eth0, SW1, gi0/1]\n"
    )

    with patch(
        "app.generate_jobs.TopologyGeneratorClient.generate_topology",
        return_value={"valid": True, "warnings": [], "yaml": duplicate_interface_yaml},
    ):
        generate_jobs.run_generate_job(app, job, "prompt")

    assert job.status == "error"
    assert "2 attempt" in job.error
    assert "last problem:" in job.error
    assert "used twice" in job.error


def test_run_generate_job_error_includes_the_generators_own_warnings(app_state):
    app = _fake_app(app_state)
    job = generate_jobs.GenerateJob(id="job-1", max_attempts=1)

    with patch(
        "app.generate_jobs.TopologyGeneratorClient.generate_topology",
        return_value={
            "valid": False,
            "warnings": [
                "Image 'X' (role=PC) is not in the list of available GNS3 templates."
            ],
        },
    ):
        generate_jobs.run_generate_job(app, job, "prompt")

    assert job.status == "error"
    assert "Image 'X' (role=PC) is not in the list" in job.error


def test_run_generate_job_maps_request_failures_to_error_without_retrying(app_state):
    app = _fake_app(app_state)
    job = generate_jobs.GenerateJob(id="job-1", max_attempts=3)

    with patch(
        "app.generate_jobs.TopologyGeneratorClient.generate_topology",
        side_effect=TimeoutError("generator API slow"),
    ) as fake_generate:
        generate_jobs.run_generate_job(app, job, "prompt")

    assert job.status == "error"
    assert job.error == "generator API slow"
    fake_generate.assert_called_once()


def test_start_job_runs_in_the_background_and_completes(app_state):
    app = _fake_app(app_state)

    with patch(
        "app.generate_jobs.TopologyGeneratorClient.generate_topology",
        return_value=_valid_result(),
    ):
        job = generate_jobs.start_job(app, "a small network")

        deadline = time.monotonic() + 2
        while job.status == "running" and time.monotonic() < deadline:
            time.sleep(0.01)

    assert job.status == "done"
    assert app_state.generate_jobs[job.id] is job


def test_prune_finished_drops_oldest_finished_jobs_but_keeps_running_ones():
    jobs = {
        f"finished-{i}": generate_jobs.GenerateJob(id=f"finished-{i}", status="done")
        for i in range(generate_jobs._MAX_FINISHED_JOBS + 10)
    }
    jobs["still-running"] = generate_jobs.GenerateJob(
        id="still-running", status="running"
    )

    generate_jobs._prune_finished(jobs)

    assert "still-running" in jobs
    assert sum(1 for job in jobs.values() if job.status != "running") == (
        generate_jobs._MAX_FINISHED_JOBS
    )
    assert "finished-0" not in jobs
    assert f"finished-{generate_jobs._MAX_FINISHED_JOBS + 9}" in jobs


def test_attachment_job_posts_multipart_and_preserves_local_model_choice(app_state):
    app = _fake_app(app_state)
    job = generate_jobs.GenerateJob(id="attachment-job")
    files = [("network.pdf", b"%PDF-1.7")]
    with patch("app.generate_jobs.requests.post") as post:
        post.return_value.status_code = 200
        post.return_value.json.return_value = _valid_result()
        generate_jobs.run_generate_job(app, job, "", files, True)

    assert job.status == "done"
    assert set(app_state.design.nodes) == {"R1", "PC1"}
    assert post.call_args.args[0].endswith("/api/generate-topology/attachments")
    assert post.call_args.kwargs["data"] == {"prompt": "", "ollama_only": "true"}
    assert post.call_args.kwargs["files"] == [("files", files[0])]


def test_screenshot_job_preserves_positions_and_aspect_ratio(app_state):
    app = _fake_app(app_state)
    job = generate_jobs.GenerateJob(id="screenshot-job")
    files = [("network.png", b"image")]
    with patch("app.generate_jobs.requests.post") as post:
        post.return_value.status_code = 200
        post.return_value.json.return_value = {
            **_valid_result(),
            "screenshot_layout": {
                "width": 800,
                "height": 400,
                "positions": {"R1": [250, 250], "PC1": [750, 750]},
            },
        }
        generate_jobs.run_generate_job(
            app, job, "", files, match_screenshot_layout=True
        )

    assert job.status == "done"
    assert post.call_args.kwargs["data"]["match_screenshot_layout"] == "true"
    assert app_state.design.layout == {
        "R1": {"x": 400.0, "y": 250.0},
        "PC1": {"x": 1200.0, "y": 650.0},
    }


def test_missing_screenshot_layout_does_not_replace_the_current_design(app_state):
    app = _fake_app(app_state)
    previous_design = app_state.design
    job = generate_jobs.GenerateJob(id="missing-layout-job")
    with patch(
        "app.generate_jobs._generate_from_attachments", return_value=_valid_result()
    ):
        generate_jobs.run_generate_job(
            app, job, "", [("network.png", b"image")], match_screenshot_layout=True
        )

    assert job.status == "error"
    assert "did not return screenshot positions" in job.error
    assert app_state.design is previous_design
