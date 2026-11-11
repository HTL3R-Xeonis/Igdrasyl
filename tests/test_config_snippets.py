import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import config_snippets


def test_save_list_update_and_delete():
    name = "router-base.v1.json"
    assert config_snippets.list_snippets() == []
    config_snippets.save(name, "Router base", ["show version", " description Test"])
    assert config_snippets.list_snippets() == [
        {"name": name, "label": "Router base", "command_count": 2}
    ]
    folder = Path(os.environ["TOPOLOGYOPERATOR_CONFIG_SNIPPETS_DIR"])
    assert json.loads((folder / name).read_text(encoding="utf-8")) == {
        "label": "Router base",
        "commands": ["show version", " description Test"],
    }
    config_snippets.save(name, "Router info", ["show interfaces"])
    assert config_snippets.get(name).commands == ["show interfaces"]
    config_snippets.delete(name)
    assert config_snippets.list_snippets() == []
    with pytest.raises(FileNotFoundError):
        config_snippets.get(name)
    with pytest.raises(FileNotFoundError):
        config_snippets.delete(name)


@pytest.mark.parametrize(
    "name", ["../x.json", "a\\b.json", "x.json\n", "x.yml", "NUL.json"]
)
def test_unsafe_names_are_rejected(name):
    with pytest.raises(config_snippets.InvalidSnippet):
        config_snippets.save(name, "Test", ["show version"])
    with pytest.raises(config_snippets.InvalidSnippet):
        config_snippets.get(name)
    with pytest.raises(config_snippets.InvalidSnippet):
        config_snippets.delete(name)


def test_invalid_update_preserves_the_existing_snippet():
    config_snippets.save("router.json", "Router", ["show version"])
    with pytest.raises(ValidationError):
        config_snippets.save("router.json", "Router", [" "])
    assert config_snippets.get("router.json").commands == ["show version"]


def test_corrupt_file_does_not_break_library():
    folder = Path(os.environ["TOPOLOGYOPERATOR_CONFIG_SNIPPETS_DIR"])
    folder.mkdir()
    (folder / "broken.json").write_bytes(b"{\xff")
    config_snippets.save("router.json", "Router", ["show version"])
    assert [entry["name"] for entry in config_snippets.list_snippets()] == [
        "router.json"
    ]
    with pytest.raises(config_snippets.InvalidSnippet):
        config_snippets.get("broken.json")
