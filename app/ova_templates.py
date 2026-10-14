"""Path-free access to OVA templates from the sister template API."""

from __future__ import annotations

import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

import requests
import yaml
from src.settings import Settings

_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_CHUNK_SIZE = 8 * 1024 * 1024
_selected_template_id: str | None = None


def initialise(settings_path: str | None) -> None:
    """Load the selected OVA resource ID without exposing or retaining a path."""
    global _selected_template_id
    if not settings_path:
        _selected_template_id = None
        return
    data = yaml.safe_load(Path(settings_path).read_text(encoding="utf-8")) or {}
    esxi = data.get("esxi") or {}
    _selected_template_id = esxi.get("gns3_vm_template_id")


def selected_template_id() -> str | None:
    return _selected_template_id or getattr(Settings.ESXI, "GNS3_VM_OVA_PATH", None)


def _validate_id(template_id: str) -> None:
    if not _ID_PATTERN.fullmatch(template_id):
        raise ValueError("Invalid OVA template ID")


def _catalog() -> list[dict]:
    try:
        response = requests.get(
            f"{Settings.API.ESXI_TEMPLATE_SERVER_URL}/api/templates", timeout=(3, 15)
        )
        response.raise_for_status()
        templates = response.json().get("templates", [])
    except (requests.RequestException, ValueError, AttributeError) as exc:
        raise RuntimeError("OVA template service is unavailable") from exc
    if not isinstance(templates, list):
        raise TypeError("OVA template service returned an invalid catalog")
    return [item for item in templates if isinstance(item, dict)]


def list_public() -> list[dict]:
    """Return only safe metadata; filenames, source directories and secrets stay hidden."""
    public = []
    for item in _catalog():
        template_id = item.get("id")
        name = item.get("name")
        if not isinstance(template_id, str) or not isinstance(name, str):
            continue
        if not _ID_PATTERN.fullmatch(template_id):
            continue
        tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        classification = " ".join(
            [name, str(item.get("category") or ""), *(str(tag) for tag in tags)]
        ).casefold()
        if "gns3" not in classification:
            continue
        public.append(
            {
                "id": template_id,
                "name": name,
                "category": item.get("category"),
                "size": item.get("size"),
                "tags": tags,
            }
        )
    return public


def display_name(template_id: str) -> str:
    _validate_id(template_id)
    for item in _catalog():
        if item.get("id") == template_id:
            return str(item.get("name") or "OVA template")
    raise FileNotFoundError("OVA template not found")


@contextmanager
def downloaded(template_id: str):
    """Stream a selected API resource to a private temporary file for import."""
    if os.path.isabs(template_id):
        yield template_id
        return
    _validate_id(template_id)
    fd, filename = tempfile.mkstemp(prefix="topologyoperator-ova-", suffix=".ova")
    os.close(fd)
    path = Path(filename)
    try:
        try:
            with requests.get(
                f"{Settings.API.ESXI_TEMPLATE_SERVER_URL}/api/download/{template_id}",
                stream=True,
                timeout=(5, 300),
            ) as response:
                if response.status_code == 404:
                    raise FileNotFoundError("OVA template not found")
                response.raise_for_status()
                with path.open("wb") as stream:
                    for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                        if chunk:
                            stream.write(chunk)
        except FileNotFoundError:
            raise
        except requests.RequestException as exc:
            raise RuntimeError("Could not download the selected OVA template") from exc
        yield str(path)
    finally:
        path.unlink(missing_ok=True)
