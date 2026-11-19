"""Tests app/users.py - password hashing and the account store, no HTTP involved."""

from __future__ import annotations

import pytest

from app import users


@pytest.fixture(autouse=True)
def _users_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_USERS_FILE", str(tmp_path / "users.json"))


def test_hash_password_round_trips_and_salts_differently():
    first = users.hash_password("hunter2!!")
    second = users.hash_password("hunter2!!")
    assert first != second  # different random salt each time
    assert users.verify_password("hunter2!!", first)
    assert users.verify_password("hunter2!!", second)
    assert not users.verify_password("wrong", first)


def test_verify_password_rejects_garbage_without_raising():
    assert not users.verify_password("anything", "not-a-real-hash")
    assert not users.verify_password("anything", "")


def test_create_list_and_delete_user():
    users.create_user("alice", "correct-horse", "user")
    users.create_user("bob", "correct-horse", "admin")

    listed = {entry["username"]: entry["role"] for entry in users.list_users()}
    assert listed == {"alice": "user", "bob": "admin"}
    assert "password_hash" not in users.list_users()[0]

    users.delete_user("alice")
    assert [entry["username"] for entry in users.list_users()] == ["bob"]


def test_create_user_rejects_a_duplicate_username():
    users.create_user("alice", "correct-horse", "user")
    with pytest.raises(ValueError, match="already exists"):
        users.create_user("alice", "another-pass", "user")


@pytest.mark.parametrize(
    "username,password,role",
    [
        ("", "correct-horse", "user"),
        ("has space", "correct-horse", "user"),
        ("alice", "short", "user"),
        ("alice", "correct-horse", "superadmin"),
    ],
)
def test_create_user_rejects_bad_input(username, password, role):
    with pytest.raises(ValueError):
        users.create_user(username, password, role)


def test_create_user_accepts_the_readonly_role():
    created = users.create_user("observer", "correct-horse", "readonly")
    assert created["role"] == "readonly"
    assert users.authenticate("observer", "correct-horse") == "readonly"


def test_authenticate_returns_role_on_success_and_none_otherwise():
    users.create_user("alice", "correct-horse", "user")
    assert users.authenticate("alice", "correct-horse") == "user"
    assert users.authenticate("alice", "wrong") is None
    assert users.authenticate("nope", "correct-horse") is None


def test_delete_and_demote_refuse_to_remove_the_last_admin():
    users.create_user("admin1", "correct-horse", "admin")
    with pytest.raises(ValueError, match="last remaining admin"):
        users.delete_user("admin1")
    with pytest.raises(ValueError, match="last remaining admin"):
        users.set_role("admin1", "user")

    users.create_user("admin2", "correct-horse", "admin")
    users.set_role("admin1", "user")  # fine now - admin2 still exists
    assert users.authenticate("admin1", "correct-horse") == "user"


def test_set_role_and_set_password_update_existing_user():
    users.create_user("alice", "correct-horse", "user")
    users.create_user("backup-admin", "correct-horse", "admin")

    updated = users.set_role("alice", "admin")
    assert updated["role"] == "admin"

    users.set_password("alice", "new-password-1")
    assert users.authenticate("alice", "new-password-1") == "admin"
    assert users.authenticate("alice", "correct-horse") is None


def test_set_role_and_delete_404_for_an_unknown_user():
    with pytest.raises(FileNotFoundError):
        users.set_role("ghost", "admin")
    with pytest.raises(FileNotFoundError):
        users.delete_user("ghost")
    with pytest.raises(FileNotFoundError):
        users.set_password("ghost", "whatever123")


def test_ensure_bootstrap_admin_only_creates_once():
    password = users.ensure_bootstrap_admin()
    assert password == "admin"
    assert users.authenticate("admin", password) == "admin"

    assert users.ensure_bootstrap_admin() is None  # already has accounts


# --- allowed_hosts -------------------------------------------------------


def test_create_user_defaults_to_all_hosts_and_round_trips_a_list():
    default = users.create_user("alice", "correct-horse", "user")
    assert default["allowed_hosts"] == "ALL"

    restricted = users.create_user(
        "bob", "correct-horse", "user", allowed_hosts=["lab-a.yml", "lab-b.yml"]
    )
    assert restricted["allowed_hosts"] == ["lab-a.yml", "lab-b.yml"]
    assert users.get_user("bob")["allowed_hosts"] == ["lab-a.yml", "lab-b.yml"]


def test_an_existing_user_json_without_the_field_reads_back_as_all(tmp_path):
    store = tmp_path / "users.json"
    store.write_text(
        '{"alice": {"password_hash": "x", "role": "user", "created_at": 1.0}}'
    )
    assert users.list_users()[0]["allowed_hosts"] == "ALL"
    assert users.get_user("alice")["allowed_hosts"] == "ALL"


def test_set_allowed_hosts_round_trips_and_validates():
    users.create_user("alice", "correct-horse", "user")

    updated = users.set_allowed_hosts("alice", ["lab-a.yml"])
    assert updated["allowed_hosts"] == ["lab-a.yml"]
    assert users.set_allowed_hosts("alice", "ALL")["allowed_hosts"] == "ALL"

    with pytest.raises(ValueError):
        users.set_allowed_hosts("alice", "not-all-and-not-a-list")
    with pytest.raises(ValueError):
        users.set_allowed_hosts("alice", ["../escape.yml"])
    with pytest.raises(ValueError):
        users.set_allowed_hosts("alice", [123])
    with pytest.raises(FileNotFoundError):
        users.set_allowed_hosts("ghost", "ALL")


def test_set_allowed_hosts_deduplicates_and_caps_the_count():
    users.create_user("alice", "correct-horse", "user")
    updated = users.set_allowed_hosts("alice", ["a.yml", "a.yml", "b.yml"])
    assert updated["allowed_hosts"] == ["a.yml", "b.yml"]

    too_many = [f"host{i}.yml" for i in range(201)]
    with pytest.raises(ValueError, match="200"):
        users.set_allowed_hosts("alice", too_many)


def test_can_access_host_admin_always_true_regardless_of_their_own_list():
    users.create_user(
        "admin1", "correct-horse", "admin", allowed_hosts=["only-this.yml"]
    )
    assert users.can_access_host("admin1", "literally-anything.yml") is True


def test_can_access_host_all_vs_explicit_list():
    users.create_user("alice", "correct-horse", "user")  # default ALL
    users.create_user("bob", "correct-horse", "user", allowed_hosts=["lab-a.yml"])

    assert users.can_access_host("alice", "lab-a.yml") is True
    assert users.can_access_host("alice", "anything-else.yml") is True

    assert users.can_access_host("bob", "lab-a.yml") is True
    assert users.can_access_host("bob", "lab-b.yml") is False


def test_can_access_host_accepts_a_record_dict_too():
    record = {"role": "user", "allowed_hosts": ["lab-a.yml"]}
    assert users.can_access_host(record, "lab-a.yml") is True
    assert users.can_access_host(record, "lab-b.yml") is False
    assert (
        users.can_access_host({"role": "admin", "allowed_hosts": []}, "x.yml") is True
    )


# --- bulk import -----------------------------------------------------------


def test_create_users_bulk_happy_path():
    results = users.create_users_bulk(
        "alice;secret123\nbob;anotherpass\n", "user", "ALL"
    )
    assert [r["status"] for r in results] == ["created", "created"]
    assert {u["username"] for u in users.list_users()} == {"alice", "bob"}


def test_create_users_bulk_skips_blank_lines():
    results = users.create_users_bulk("\nalice;secret123\n\n  \n", "user", "ALL")
    assert len(results) == 1
    assert results[0]["status"] == "created"


def test_create_users_bulk_reports_a_missing_semicolon_without_stopping_the_batch():
    results = users.create_users_bulk(
        "alice;secret123\nthis-line-has-no-separator\nbob;anotherpass", "user", "ALL"
    )
    assert [r["status"] for r in results] == ["created", "error", "created"]
    assert "';'" in results[1]["detail"]
    assert {u["username"] for u in users.list_users()} == {"alice", "bob"}


def test_create_users_bulk_rejects_a_duplicate_within_the_batch():
    results = users.create_users_bulk("alice;one12345\nalice;two12345", "user", "ALL")
    assert results[0]["status"] == "created"
    assert results[1]["status"] == "error"
    assert "Duplicate" in results[1]["detail"]


def test_create_users_bulk_reports_but_does_not_stop_on_an_existing_username():
    users.create_user("alice", "correct-horse", "user")
    results = users.create_users_bulk("alice;one12345\nbob;two12345", "user", "ALL")
    assert results[0]["status"] == "error"
    assert "already exists" in results[0]["detail"]
    assert results[1]["status"] == "created"


def test_create_users_bulk_password_may_contain_a_semicolon_or_any_character():
    results = users.create_users_bulk("alice;pa;ss;word!äÖü😀", "user", "ALL")
    assert results[0]["status"] == "created"
    assert users.authenticate("alice", "pa;ss;word!äÖü😀") == "user"


def test_create_users_bulk_applies_the_shared_role_and_allowed_hosts():
    results = users.create_users_bulk(
        "alice;secret123\nbob;anotherpass", "admin", ["lab-a.yml"]
    )
    assert all(r["allowed_hosts"] == ["lab-a.yml"] for r in results)
    assert all(r["role"] == "admin" for r in results)


def test_ensure_bootstrap_admin_uses_a_password_from_the_environment_and_never_returns_it(
    monkeypatch,
):
    monkeypatch.setenv(users.BOOTSTRAP_PASSWORD_ENV_VAR, "chosen-by-the-installer")

    assert (
        users.ensure_bootstrap_admin() is None
    )  # nothing to log: the value is not the default
    assert users.authenticate("admin", "chosen-by-the-installer") == "admin"
    assert users.authenticate("admin", "admin") is None
