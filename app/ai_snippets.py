"""Compile AI-generated settings into snippets for explicitly enabled exercises."""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import requests
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from src.graph.environment import Environment
from src.settings import Settings

from app import config_snippets, fix_jobs, push_jobs

Topic = Literal["vlan", "interface_description"]
TOPICS = {
    "vlan": "Cisco IOS · VLAN",
    "interface_description": "Cisco IOS · Port description",
}
_SLOTS = threading.BoundedSemaphore(2)
_DRAFT_LOCK = threading.Lock()
_DRAFT_TTL = 600
_MAX_DRAFTS = 100
_IOSV_IMAGE = re.compile(r"(?:Cisco\s+)?IOSv(L2)?(?:\s+.*)?", re.IGNORECASE)


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
    device: str = Field(min_length=1, max_length=200)
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


class TargetChanged(Exception):
    """The device or the project changed after it was selected."""


class DraftExpired(Exception):
    """The reviewed draft expired or was already pushed."""


class TopicDisabled(Exception):
    """The topic is no longer available."""


@dataclass(frozen=True)
class Draft:
    target: fix_jobs.FixTarget
    topic: Topic
    commands: tuple[str, ...]
    expires: float


def _supports(topic: Topic, node) -> bool:
    image = _IOSV_IMAGE.fullmatch(node.image)
    return (
        node.env == Environment.ON_GNS3
        and image is not None
        and (topic != "vlan" or image[1] is not None)
    )


def targets(app: FastAPI, topic: Topic) -> list[str]:
    graph = getattr(app.state, "graph", None)
    if (
        graph is None
        or not getattr(app.state, "project_id", None)
        or not getattr(app.state, "gns3_vm_ip", None)
    ):
        return []
    return sorted(name for name, node in graph.nodes.items() if _supports(topic, node))


def _target(app: FastAPI, topic: Topic, name: str) -> fix_jobs.FixTarget:
    if name not in targets(app, topic):
        raise TargetChanged("Choose a compatible device in the active GNS3 project.")
    target = fix_jobs.current_target(app, name)
    if target is None:
        raise TargetChanged("The device is no longer available.")
    return target


def _generate_settings(request: GenerateRequest) -> dict:
    url = _generator_url()
    if not _SLOTS.acquire(blocking=False):
        raise Busy
    try:
        response = requests.post(
            f"{url}/api/config-snippets/generate",
            json=request.model_dump(exclude={"device"}),
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


def generate(app: FastAPI, request: GenerateRequest) -> dict:
    target = _target(app, request.topic, request.device)
    settings = _generate_settings(request)
    snippet = compile_snippet(request.topic, settings)
    if _target(app, request.topic, request.device) != target:
        raise TargetChanged("The project or device changed. Generate a new draft.")
    draft_id = uuid.uuid4().hex
    with _DRAFT_LOCK:
        if not hasattr(app.state, "ai_snippet_drafts"):
            app.state.ai_snippet_drafts = {}
        drafts = app.state.ai_snippet_drafts
        now = time.monotonic()
        for key, draft in list(drafts.items()):
            if draft.expires <= now:
                del drafts[key]
        if len(drafts) >= _MAX_DRAFTS:
            del drafts[next(iter(drafts))]
        drafts[draft_id] = Draft(
            target, request.topic, tuple(snippet.commands), now + _DRAFT_TTL
        )
    return {"draft_id": draft_id, "device": request.device, **snippet.model_dump()}


def push_draft(app: FastAPI, draft_id: str) -> push_jobs.PushJob:
    with _DRAFT_LOCK:
        drafts = getattr(app.state, "ai_snippet_drafts", {})
        draft = drafts.get(draft_id)
        if draft is None or draft.expires <= time.monotonic():
            raise DraftExpired

        def verify_target() -> None:
            if draft.topic not in enabled_topics():
                raise TopicDisabled("This exercise is no longer enabled.")
            if _target(app, draft.topic, draft.target.device) != draft.target:
                raise TargetChanged(
                    "The project or device changed. Generate a new draft."
                )

        verify_target()
        job = push_jobs.start_job(
            app,
            {draft.target.device: list(draft.commands)},
            wait_seconds=0,
            audit_note="AI snippet",
            verify_target=verify_target,
        )
        del drafts[draft_id]
        return job
