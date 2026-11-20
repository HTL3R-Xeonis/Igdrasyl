"""The canvas files of one host profile (app/profile_state.py) and the launch script that points a backend at them."""

import importlib.util
from pathlib import Path

import pytest

from app import profile_state

LAUNCH = Path(__file__).resolve().parent.parent / "deploy" / "xeonis-backend-launch.py"


@pytest.fixture
def launch():
    spec = importlib.util.spec_from_file_location("xeonis_backend_launch", LAUNCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for variable in profile_state.FILES:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_launch_script_and_app_name_the_same_files(launch):
    assert launch.PER_PROFILE_FILES == profile_state.FILES


def test_every_file_of_the_set_is_the_one_a_module_reads():
    """The variables must be the ones the modules really read, with the same default file name."""
    from app import design_store, holds, label_layout, layout, node_notes

    assert profile_state.FILES == {
        design_store._DESIGN_FILE_ENV_VAR: design_store._DEFAULT_DESIGN_FILE,
        holds._HOLDS_FILE_ENV_VAR: holds._DEFAULT_HOLDS_FILE,
        label_layout._LABELS_FILE_ENV_VAR: label_layout._DEFAULT_LABELS_FILE,
        layout._LAYOUT_FILE_ENV_VAR: layout._DEFAULT_LAYOUT_FILE,
        node_notes._NOTES_FILE_ENV_VAR: node_notes._DEFAULT_NOTES_FILE,
    }


def test_first_start_copies_the_shared_files_and_leaves_them(
    launch, clean_env, monkeypatch
):
    profiles = clean_env / "profiles"
    profiles.mkdir()
    (clean_env / "design_state.json").write_text('{"shared": 1}')
    (clean_env / "layout.json").write_text('{"x": 1}')

    launch.point_state_files_at_the_profile(profiles, "a.yml")

    import os

    state = profiles / ".state" / "a.yml"
    assert (state / "design_state.json").read_text() == '{"shared": 1}'
    assert (state / "layout.json").read_text() == '{"x": 1}'
    assert not (state / "holds.json").exists()  # nothing shared to copy
    assert os.environ["TOPOLOGYOPERATOR_DESIGN_FILE"] == str(
        state / "design_state.json"
    )
    assert os.environ["TOPOLOGYOPERATOR_HOLDS_FILE"] == str(state / "holds.json")
    assert (clean_env / "design_state.json").read_text() == '{"shared": 1}'


def test_each_host_then_changes_only_its_own_file(launch, clean_env, monkeypatch):
    profiles = clean_env / "profiles"
    profiles.mkdir()
    (clean_env / "design_state.json").write_text('{"shared": 1}')

    launch.point_state_files_at_the_profile(profiles, "a.yml")
    (profiles / ".state" / "a.yml" / "design_state.json").write_text('{"a": 2}')
    for variable in profile_state.FILES:
        monkeypatch.delenv(variable)
    launch.point_state_files_at_the_profile(profiles, "b.yml")

    assert (profiles / ".state" / "b.yml" / "design_state.json").read_text() == (
        '{"shared": 1}'
    )
    assert (profiles / ".state" / "a.yml" / "design_state.json").read_text() == (
        '{"a": 2}'
    )


def test_a_later_start_keeps_what_the_host_saved(launch, clean_env, monkeypatch):
    profiles = clean_env / "profiles"
    profiles.mkdir()
    (clean_env / "layout.json").write_text('{"shared": 1}')
    launch.point_state_files_at_the_profile(profiles, "a.yml")
    own = profiles / ".state" / "a.yml" / "layout.json"
    own.write_text('{"mine": 1}')
    (clean_env / "layout.json").write_text('{"shared": 2}')
    for variable in profile_state.FILES:
        monkeypatch.delenv(variable)

    launch.point_state_files_at_the_profile(profiles, "a.yml")

    assert own.read_text() == '{"mine": 1}'


def test_the_shared_file_named_by_a_variable_is_the_source(
    launch, clean_env, monkeypatch
):
    profiles = clean_env / "profiles"
    profiles.mkdir()
    shared = clean_env / "elsewhere.json"
    shared.write_text('{"x": 9}')
    monkeypatch.setenv("TOPOLOGYOPERATOR_NOTES_FILE", str(shared))

    launch.point_state_files_at_the_profile(profiles, "a.yml")

    assert (profiles / ".state" / "a.yml" / "node_notes.json").read_text() == (
        '{"x": 9}'
    )


def test_state_dir_rejects_an_unsafe_name(clean_env, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR", str(clean_env))
    with pytest.raises(Exception, match="valid profile filename"):
        profile_state.state_dir("../evil.yml")
