"""Compile AI-generated settings into snippets for explicitly enabled exercises."""

from __future__ import annotations

import os
import threading
import uuid
from typing import Literal
from urllib.parse import urlsplit

import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from src.settings import Settings

from app import config_snippets

Topic = Literal["vlan", "interface_description"]
TOPICS = {
    "vlan": "Cisco IOS · VLAN",
    "interface_description": "Cisco IOS · Port description",
}
_SLOTS = threading.BoundedSemaphore(2)


def service_token() -> str:
    return os.environ.get("XEONIS_AI_SNIPPETS_TOKEN", "")


def classroom_mode() -> bool:
    return os.environ.get("XEONIS_CLASSROOM_MODE") == "1"


def _generator_url() -> str:
    # Keep the service token independent of the Generator URL editable in host profiles.
    url = os.environ.get("TOPOLOGYOPERATOR_AI_SNIPPET_GENERATOR_URL", "").strip()
    try:
        parts = urlsplit(url)
        if (
            parts.scheme in {"http", "https"}
            and parts.hostname
            and parts.username is None
            and not parts.query
            and not parts.fragment
        ):
            return url.rstrip("/")
    except ValueError:
        pass
    return ""


def enabled_topics() -> list[str]:
    token = service_token()
    if (
        len(token) < 32
        or not token.isascii()
        or not token.isprintable()
        or " " in token
        or not _generator_url()
    ):
        return []
    configured = os.environ.get("TOPOLOGYOPERATOR_AI_SNIPPET_TOPICS", "").split(",")
    requested = {value.strip() for value in configured}
    return [topic for topic in TOPICS if topic in requested]


class GenerateRequest(BaseModel):
    topic: Topic
    prompt: str = Field(min_length=1, max_length=1500)

    @field_validator("prompt")
    @classmethod
    def trim_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter a prompt.")
        return value.strip()


class VlanSettings(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    vlan_id: int = Field(ge=2, le=4094)
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

    @field_validator("vlan_id")
    @classmethod
    def check_reserved(cls, value: int) -> int:
        if 1002 <= value <= 1005:
            raise ValueError("Use a VLAN ID outside the reserved range 1002–1005.")
        return value


class InterfaceSettings(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    interface: str = Field(
        pattern=r"^(?:GigabitEthernet|FastEthernet|TenGigabitEthernet|Ethernet)[0-9]{1,3}(?:/[0-9]{1,3}){0,2}$"
    )
    description: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")


class InvalidDraft(Exception):
    """The generator could not extract settings supported by this exercise."""


class GeneratorUnavailable(Exception):
    """The generator did not respond successfully."""


class Busy(Exception):
    """The generation slots are in use."""


def _generate_settings(request: GenerateRequest) -> dict:
    url = _generator_url()
    if not _SLOTS.acquire(blocking=False):
        raise Busy
    try:
        response = requests.post(
            f"{url}/api/config-snippets/generate",
            json=request.model_dump(),
            headers={"Authorization": f"Bearer {service_token()}"},
            timeout=min(90, Settings.API.TOPOLOGY_GENERATOR_TIMEOUT_SECONDS),
            allow_redirects=False,
        )
        if response.status_code == 422:
            raise InvalidDraft
        if response.status_code != 200:
            raise GeneratorUnavailable
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        raise GeneratorUnavailable from exc
    finally:
        _SLOTS.release()


def compile_snippet(topic: Topic, settings: dict) -> config_snippets.Snippet:
    try:
        if topic == "vlan":
            vlan = VlanSettings.model_validate(settings)
            return config_snippets.Snippet(
                label=f"VLAN {vlan.vlan_id} · {vlan.name}",
                commands=[
                    "configure terminal",
                    f"vlan {vlan.vlan_id}",
                    f"name {vlan.name}",
                    "end",
                ],
            )
        port = InterfaceSettings.model_validate(settings)
        return config_snippets.Snippet(
            label=f"Description · {port.interface}",
            commands=[
                "configure terminal",
                f"interface {port.interface}",
                f"description {port.description}",
                "end",
            ],
        )
    except ValidationError as exc:
        raise InvalidDraft from exc


def generate(request: GenerateRequest) -> dict:
    settings = _generate_settings(request)
    snippet = compile_snippet(request.topic, settings)
    return {"topic": request.topic, "settings": settings, **snippet.model_dump()}


def save(topic: Topic, settings: dict) -> str:
    snippet = compile_snippet(topic, settings)
    name = f"ai-{uuid.uuid4().hex}.json"
    config_snippets.save(name, snippet.label, snippet.commands)
    return name
