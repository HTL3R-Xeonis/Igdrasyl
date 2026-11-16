"""Tests app/backend_presence.py - the in-memory 'who's on this host' tracker, no HTTP involved."""

from __future__ import annotations

import time

import pytest

from app import backend_presence


@pytest.fixture(autouse=True)
def _reset_presence():
    backend_presence._presence.clear()
    yield
    backend_presence._presence.clear()


def test_record_and_active_users_round_trip():
    backend_presence.record("lab-a.yml", "alice")
    assert backend_presence.active_users("lab-a.yml") == ["alice"]
    assert backend_presence.count("lab-a.yml") == 1


def test_multiple_users_on_one_host():
    backend_presence.record("lab-a.yml", "alice")
    backend_presence.record("lab-a.yml", "bob")
    assert backend_presence.active_users("lab-a.yml") == ["alice", "bob"]
    assert backend_presence.count("lab-a.yml") == 2


def test_a_host_with_no_recorded_presence_is_empty():
    assert backend_presence.active_users("never-touched.yml") == []
    assert backend_presence.count("never-touched.yml") == 0


def test_stale_entries_are_pruned():
    backend_presence.record("lab-a.yml", "alice")
    backend_presence._presence["lab-a.yml"]["bob"] = time.monotonic() - 400
    assert backend_presence.active_users("lab-a.yml", within_seconds=300) == ["alice"]


def test_recording_again_refreshes_the_last_seen_time():
    backend_presence._presence.setdefault("lab-a.yml", {})["alice"] = (
        time.monotonic() - 400
    )
    backend_presence.record("lab-a.yml", "alice")
    assert backend_presence.active_users("lab-a.yml", within_seconds=300) == ["alice"]


def test_different_hosts_are_tracked_independently():
    backend_presence.record("lab-a.yml", "alice")
    backend_presence.record("lab-b.yml", "bob")
    assert backend_presence.active_users("lab-a.yml") == ["alice"]
    assert backend_presence.active_users("lab-b.yml") == ["bob"]
