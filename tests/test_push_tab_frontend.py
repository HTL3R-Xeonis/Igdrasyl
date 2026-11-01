"""
The Config Push tab shows which devices a running push is waiting for. There is no
browser in the test setup, so the small pure text function is cut out of app.js and run
with node (skipped where node is missing); the rest is checked against the files.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import app.config_push as cp
import app.main as main_module
from app import push_jobs

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def _waiting_text(names) -> str:
    source = (STATIC / "app.js").read_text()
    match = re.search(
        r"^function pushWaitingText\(.*?^\}", source, re.DOTALL | re.MULTILINE
    )
    assert match, "pushWaitingText is missing from app.js"
    script = (
        f"{match.group(0)}\nprocess.stdout.write(pushWaitingText({json.dumps(names)}));"
    )
    return subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    ).stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_waiting_line_names_the_devices_and_is_empty_without_any():
    assert (
        _waiting_text(["SW-CORE", "SW-DMZ"])
        == "Waiting for: SW-CORE, SW-DMZ (still booting)"
    )
    assert _waiting_text(["SW-CORE"]) == "Waiting for: SW-CORE (still booting)"
    assert _waiting_text([]) == ""
    assert _waiting_text(None) == ""


def test_the_progress_view_uses_the_waiting_line_and_the_scripts_got_a_new_version():
    source = (STATIC / "app.js").read_text()
    progress = source[source.index("function renderPushProgress") :]
    progress = progress[: progress.index("\n}\n")]
    assert "pushWaitingText(job.waiting_for)" in progress
    html = (STATIC / "index.html").read_text()
    assert "/app.js?v=linkhit-1" in html


def test_the_job_status_endpoint_hands_waiting_for_to_the_page(client, app_state):
    def fake_waiting(devices, app, wait_seconds, on_progress=None, on_waiting=None):
        on_waiting(["SW-CORE", "SW-DMZ"])
        job_id = main_module.app.state.push_job.id
        seen.append(client.get(f"/config-push/jobs/{job_id}").json())
        return [cp.DeviceResult(device="SW-CORE", transport="netmiko", ok=True)]

    seen: list[dict] = []
    with patch.object(push_jobs, "push_all_waiting", fake_waiting):
        job = push_jobs.start_job(main_module.app, {"SW-CORE": ["a"]})
        for _ in range(100):
            if job.status != "running":
                break
            time.sleep(0.05)
    assert seen and seen[0]["status"] == "running"
    assert seen[0]["waiting_for"] == ["SW-CORE", "SW-DMZ"]
    assert client.get(f"/config-push/jobs/{job.id}").json()["waiting_for"] == []
