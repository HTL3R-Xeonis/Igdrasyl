"""Tests app/audit_log.py - the small, named-events-only admin log, no HTTP involved."""

from __future__ import annotations

from app import audit_log


def test_record_and_recent_round_trip():
    audit_log.record("login_success", "alice")
    audit_log.record("user_created", "admin", "created user bob")

    events = audit_log.recent()
    assert [e["event"] for e in events] == [
        "user_created",
        "login_success",
    ]  # newest first
    assert events[0]["username"] == "admin"
    assert events[0]["detail"] == "created user bob"
    assert events[1]["detail"] == ""
    assert isinstance(events[1]["ts"], float)


def test_recent_respects_the_limit():
    for i in range(5):
        audit_log.record("login_success", f"user{i}")

    events = audit_log.recent(limit=2)
    assert [e["username"] for e in events] == ["user4", "user3"]


def test_recent_is_empty_when_nothing_was_ever_recorded():
    assert audit_log.recent() == []


def test_a_malformed_line_is_skipped_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_AUDIT_LOG_FILE", str(tmp_path / "audit.log"))
    audit_log.record("login_success", "alice")
    with open(tmp_path / "audit.log", "a") as f:
        f.write("not valid json\n")
    audit_log.record("login_success", "bob")

    events = audit_log.recent()
    assert [e["username"] for e in events] == ["bob", "alice"]


def test_record_never_raises_when_the_log_cannot_be_written(tmp_path, monkeypatch):
    # A regular file where a directory is needed makes `mkdir(parents=True)` fail
    # reliably, regardless of whether the test happens to run as root.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("TOPOLOGYOPERATOR_AUDIT_LOG_FILE", str(blocker / "audit.log"))
    audit_log.record("login_success", "alice")  # must not raise
