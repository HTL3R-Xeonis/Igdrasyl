"""Read and publish ESXi template manifests in the configured share."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MAX_UPLOAD_BYTES = 32 * 1024**3
MAX_MANIFEST_BYTES = 1024**2
STORE_LOCK = Lock()

Label = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]


class Metadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Label
    category: Label
    os_family: Label
    os_version: Label
    cpu: int = Field(ge=1)
    memory: str = Field(
        pattern=r"^[1-9][0-9]*(?:[.,][0-9]+)?\s*(?:GB|MB)$", max_length=20
    )
    disk: str = Field(
        pattern=r"^[1-9][0-9]*(?:[.,][0-9]+)?\s*(?:GB|MB|TB)$", max_length=20
    )
    hostname: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=253)
    ]
    domain: str | None = Field(default=None, max_length=253)
    tags: list[Label] = Field(default_factory=list, max_length=30)
    users: list[Label] | None = Field(default=None, max_length=30)
    note: str = Field(default="", max_length=1200)

    def fields(self) -> dict[str, Any]:
        entry = self.model_dump(exclude={"os_family", "os_version", "users"})
        entry["os"] = {"family": self.os_family, "version": self.os_version}
        if self.users is not None:
            entry["users"] = self.users
        return entry


class StoreError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def store_path() -> Path | None:
    configured = os.getenv("TOPOLOGYOPERATOR_TEMPLATE_SHARE_DIR")
    if not configured:
        return None
    path = Path(configured)
    if not path.is_absolute() or not path.is_dir():
        raise StoreError("The configured template share is unavailable.", 503)
    return path.resolve()


def refresh_minutes() -> int | None:
    value = os.getenv("TOPOLOGYOPERATOR_TEMPLATE_REFRESH_MINUTES", "")
    return (
        int(value) if value.isascii() and value.isdecimal() and len(value) < 7 else None
    )


def _read(path: Path) -> tuple[dict, str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Not a regular manifest")
    with path.open("rb") as manifest:
        raw = manifest.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("Manifest too large")
    document = yaml.safe_load(raw)
    if not isinstance(document, dict) or not isinstance(
        document.get("templates"), list
    ):
        raise TypeError("Invalid manifest")
    return document, hashlib.sha256(raw).hexdigest()


def _manifest_path(root: Path, relative: str) -> Path:
    key = PurePosixPath(relative)
    if (
        key.is_absolute()
        or "\\" in relative
        or not key.parts
        or any(part.startswith(".") for part in key.parts)
    ):
        raise StoreError("Invalid template manifest.")
    path = root.joinpath(*key.parts)
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise StoreError("Invalid template manifest.") from exc
    if (
        path.suffix.lower() not in {".yaml", ".yml"}
        or not path.is_relative_to(root)
        or resolved != path
    ):
        raise StoreError("Invalid template manifest.")
    return path


def _row(root: Path, path: Path, revision: str, entry: dict) -> dict:
    filename = entry.get("file")
    image = path.parent / filename if isinstance(filename, str) and filename else None
    os_info = entry.get("os")
    os_info = os_info if isinstance(os_info, dict) else {}
    metadata = {
        key: _text(entry.get(key))
        for key in ("name", "category", "memory", "disk", "hostname", "domain", "note")
    }
    metadata.update(
        cpu=_text(entry.get("cpu")),
        os_family=_text(os_info.get("family")),
        os_version=_text(os_info.get("version")),
        tags=_labels(entry.get("tags")),
        users=_labels(entry.get("users")),
    )
    return {
        "id": str(entry.get("id") or ""),
        "manifest": path.relative_to(root).as_posix(),
        "revision": revision,
        "metadata": metadata,
        "file": image.name if image else None,
        "file_exists": bool(
            image
            and image.parent == path.parent
            and not image.is_symlink()
            and image.is_file()
        ),
        "size": _text(entry.get("size")),
    }


def _text(value: Any) -> str | None:
    return str(value) if isinstance(value, (str, int, float)) else None


def _labels(value: Any) -> list[str] | None:
    return (
        value
        if isinstance(value, list) and all(isinstance(item, str) for item in value)
        else None
    )


def templates(root: Path) -> list[dict]:
    rows = []
    for folder, dirs, files in os.walk(root):
        directory = Path(folder)
        dirs[:] = [
            name
            for name in dirs
            if not name.startswith(".") and not (directory / name).is_symlink()
        ]
        for filename in files:
            path = directory / filename
            if filename.startswith(".") or path.suffix.lower() not in {".yaml", ".yml"}:
                continue
            try:
                document, revision = _read(path)
            except (OSError, TypeError, ValueError, yaml.YAMLError):
                continue
            for entry in document["templates"]:
                if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                    rows.append(_row(root, path, revision, entry))
    return sorted(rows, key=lambda row: row["metadata"]["name"].casefold())


def _write(path: Path, document: dict, mode: int) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".manifest-")
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            yaml.safe_dump(document, handle, sort_keys=False, allow_unicode=True)
        temp.chmod(mode)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:100]
    if not slug:
        raise StoreError("Use at least one Latin letter or digit in the template name.")
    return slug


def validate_new_name(root: Path, name: str) -> str:
    slug = _slug(name)
    if (root / f"esxi-{slug}").exists() or any(
        row["metadata"]["name"].casefold() == name.casefold() for row in templates(root)
    ):
        raise StoreError("A template with this name already exists.", 409)
    return slug


def publish(root: Path, temp: Path, sha256: str, size: int, metadata: Metadata) -> None:
    try:
        with tarfile.open(temp, "r:") as archive:
            if not any(
                item.isfile() and item.name.lower().endswith(".ovf") for item in archive
            ):
                raise StoreError("The OVA archive does not contain an OVF descriptor.")
    except (tarfile.TarError, EOFError) as exc:
        raise StoreError("Upload a valid OVA archive.") from exc
    with STORE_LOCK:
        slug = validate_new_name(root, metadata.name)
        if any(row["id"].upper() == sha256 for row in templates(root)):
            raise StoreError("This OVA is already in the template share.", 409)
        folder = root / f"esxi-{slug}"
        try:
            folder.mkdir()
        except FileExistsError as exc:
            raise StoreError("A template with this name already exists.", 409) from exc
        filename = f"{slug}_{sha256}.ova"
        image = folder / filename
        try:
            temp.chmod(0o644)
            temp.replace(image)
            entry = {
                "id": sha256,
                "name": metadata.name,
                "file": filename,
                "size": f"{size / 2**30:.2f}".replace(".", ",") + "GB",
                "users": [],
                **metadata.fields(),
            }
            _write(folder / f"{slug}.yml", {"version": 1, "templates": [entry]}, 0o644)
        except BaseException:
            image.unlink(missing_ok=True)
            folder.rmdir()
            raise


def edit(
    root: Path, relative: str, template_id: str, revision: str, metadata: Metadata
) -> None:
    path = _manifest_path(root, relative)
    with STORE_LOCK:
        try:
            document, current_revision = _read(path)
        except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
            raise StoreError(
                "This manifest is no longer available. Refresh the list.", 409
            ) from exc
        if current_revision != revision:
            raise StoreError(
                "This manifest changed. Refresh the list and try again.", 409
            )
        matches = [
            entry
            for entry in document["templates"]
            if isinstance(entry, dict) and str(entry.get("id") or "") == template_id
        ]
        if len(matches) != 1 or matches[0].get("name") != metadata.name:
            raise StoreError(
                "The template name and identity must match the manifest.", 409
            )
        entry = matches[0]
        fields = metadata.fields()
        if isinstance(entry.get("os"), dict):
            fields["os"] = {**entry["os"], **fields["os"]}
        entry.update(fields)
        _write(path, document, stat.S_IMODE(path.stat().st_mode))
