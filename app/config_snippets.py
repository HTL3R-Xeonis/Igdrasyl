"""File-backed library of reusable config-push commands."""

from __future__ import annotations

import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import BaseModel, Field, field_validator

_DIR_ENV_VAR = "TOPOLOGYOPERATOR_CONFIG_SNIPPETS_DIR"
_SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.json$")
_RESERVED_STEM = re.compile(r"CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]", re.IGNORECASE)


class InvalidSnippet(ValueError):
    """Invalid name or file contents."""


class Snippet(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    commands: list[str] = Field(min_length=1, max_length=10_000)

    @field_validator("label")
    @classmethod
    def clean_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("enter a label")
        return value.strip()

    @field_validator("commands")
    @classmethod
    def check_commands(cls, commands: list[str]) -> list[str]:
        if any(not command.strip() for command in commands):
            raise ValueError("commands must not be blank")
        return commands


def _directory() -> Path:
    return Path(os.getenv(_DIR_ENV_VAR, "config_snippets"))


def _valid_name(name: str) -> bool:
    return bool(
        _SAFE_NAME_PATTERN.fullmatch(name)
        and not _RESERVED_STEM.fullmatch(name.split(".", 1)[0])
    )


def _path(name: str) -> Path:
    if not _valid_name(name):
        raise InvalidSnippet(
            "use a plain .json filename with letters, digits, '.', '_' or '-'"
        )
    return _directory() / name


def list_snippets() -> list[dict]:
    snippets = []
    for path in sorted(_directory().glob("*.json")):
        if not _valid_name(path.name):
            continue
        try:
            snippet = get(path.name)
        except (InvalidSnippet, OSError):
            continue
        snippets.append(
            {
                "name": path.name,
                "label": snippet.label,
                "command_count": len(snippet.commands),
            }
        )
    return snippets


def get(name: str) -> Snippet:
    path = _path(name)
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(name)
    try:
        return Snippet.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise InvalidSnippet(f"invalid snippet file {name!r}") from exc


def save(name: str, label: str, commands: list[str]) -> Snippet:
    path = _path(name)
    snippet = Snippet(label=label, commands=commands)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temp = Path(file.name)
        try:
            file.write((snippet.model_dump_json(indent=2) + "\n").encode("utf-8"))
            file.close()
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
    return snippet


def delete(name: str) -> None:
    _path(name).unlink()
