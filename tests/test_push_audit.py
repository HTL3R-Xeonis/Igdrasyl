import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app import push_audit
from app.config_push import CommandResult, DeviceResult


def test_append_read_and_trim(monkeypatch):
    monkeypatch.setattr(push_audit, "MAX_RECORDS", 3)
    assert push_audit.read(3) == []
    for i in range(5):
        push_audit.record([], f"run {i}")
    path = Path(os.environ["TOPOLOGYOPERATOR_PUSH_AUDIT_FILE"])
    assert len(path.read_text().splitlines()) == 3
    assert [row["note"] for row in push_audit.read(2)] == ["run 4", "run 3"]
    assert [row["note"] for row in push_audit.read(99)] == ["run 4", "run 3", "run 2"]


def test_records_failed_device_without_raw_commands_or_errors():
    push_audit.record(
        [
            DeviceResult(
                device="R1",
                transport="netmiko",
                ok=False,
                total_commands=3,
                executed=1,
                error="secret error",
                commands=[
                    CommandResult(
                        command="secret command", output="secret output", ok=False
                    )
                ],
            )
        ],
        "Direct push",
    )
    (entry,) = push_audit.read()
    assert entry["timestamp"].endswith("+00:00")
    assert entry["status"] == "failed"
    assert entry["devices"] == [
        {
            "device": "R1",
            "transport": "netmiko",
            "ok": False,
            "total_commands": 3,
            "executed": 1,
        }
    ]
    assert (
        "secret" not in Path(os.environ["TOPOLOGYOPERATOR_PUSH_AUDIT_FILE"]).read_text()
    )


def test_interrupted_push_is_recorded_and_reraises_original_error():
    with pytest.raises(RuntimeError, match="private"):
        with push_audit.track("Background push"):
            raise RuntimeError("private")
    (entry,) = push_audit.read()
    assert entry["status"] == "interrupted"
    assert "private" not in str(entry)


def test_parallel_writers_do_not_lose_runs_when_trimming():
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: push_audit.record([], str(i)), range(80)))
    rows = push_audit.read()
    assert len(rows) == push_audit.MAX_RECORDS
    assert len({row["note"] for row in rows}) == push_audit.MAX_RECORDS


def test_storage_error_does_not_turn_successful_push_into_failure(
    monkeypatch, tmp_path
):
    blocker = tmp_path / "regular-file"
    blocker.write_text("not a directory")
    monkeypatch.setenv("TOPOLOGYOPERATOR_PUSH_AUDIT_FILE", str(blocker / "audit.jsonl"))
    with push_audit.track("Background push"):
        pass


def test_recover_after_incomplete_line():
    path = Path(os.environ["TOPOLOGYOPERATOR_PUSH_AUDIT_FILE"])
    push_audit.record([], "first")
    with path.open("a") as file:
        file.write('{"note":"incomplete')
    push_audit.record([], "second")
    assert [row["note"] for row in push_audit.read()] == ["second", "first"]
