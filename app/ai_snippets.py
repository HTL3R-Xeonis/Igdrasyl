"""Create reviewed configuration drafts for a single live device."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from ipaddress import IPv4Interface
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import requests
from fastapi import FastAPI
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)
from src.graph.environment import Environment
from src.settings import Settings

from app import fix_jobs, push_jobs
from app.gns3_state import list_node_ports

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


def _policy_path() -> Path:
    return Path(os.getenv("TOPOLOGYOPERATOR_AI_CONFIG_FILE", "ai_config.json"))


def get_enabled() -> bool:
    try:
        return json.loads(_policy_path().read_text(encoding="utf-8")) is True
    except (ValueError, OSError):
        return False


def set_enabled(enabled: bool) -> None:
    path = _policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as file:
        temp = Path(file.name)
        try:
            json.dump(enabled, file)
            file.close()
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def available() -> bool:
    token = service_token()
    return (
        len(token) >= 32
        and token.isascii()
        and token.isprintable()
        and " " not in token
        and bool(_generator_url())
    )


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=1500)

    @field_validator("prompt")
    @classmethod
    def trim_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter a prompt.")
        return value.strip()


class VlanId(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    vlan_id: int = Field(ge=2, le=4094)

    @field_validator("vlan_id")
    @classmethod
    def check_reserved(cls, value: int) -> int:
        if 1002 <= value <= 1005:
            raise ValueError("Use a VLAN ID outside the reserved range 1002–1005.")
        return value


class VlanSettings(VlanId):
    kind: Literal["vlan"]
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


class InterfaceSettings(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    interface: str = Field(
        pattern=r"^(?:GigabitEthernet|FastEthernet|TenGigabitEthernet|Ethernet)[0-9]{1,3}(?:/[0-9]{1,3}){0,2}$"
    )


class DescriptionSettings(InterfaceSettings):
    kind: Literal["description"]
    description: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")


class AccessVlanSettings(InterfaceSettings, VlanId):
    kind: Literal["access_vlan"]


class IPv4Settings(InterfaceSettings):
    kind: Literal["ipv4"]
    address: str = Field(pattern=r"^[0-9.]{7,15}/[0-9]{1,2}$")


_Operation = Annotated[
    VlanSettings | DescriptionSettings | AccessVlanSettings | IPv4Settings,
    Field(discriminator="kind"),
]
_OPERATIONS = TypeAdapter(
    Annotated[list[_Operation], Field(min_length=1, max_length=20)]
)
_PORT_PREFIX = re.compile(
    r"^(TenGigabitEthernet|GigabitEthernet|FastEthernet|Ethernet|Te|Gi|Fa|Eth|Et|e)(?=\d)",
    re.IGNORECASE,
)
_PORT_NAMES = {
    "tengigabitethernet": "TenGigabitEthernet",
    "te": "TenGigabitEthernet",
    "gigabitethernet": "GigabitEthernet",
    "gi": "GigabitEthernet",
    "fastethernet": "FastEthernet",
    "fa": "FastEthernet",
    "ethernet": "Ethernet",
    "eth": "Ethernet",
    "et": "Ethernet",
    "e": "Ethernet",
}


def _canonical_port(port: str) -> str:
    return _PORT_PREFIX.sub(lambda match: _PORT_NAMES[match[0].lower()], port)


class InvalidDraft(Exception):
    """The proposed settings cannot be applied to this device."""


class GeneratorUnavailable(Exception):
    """The generator did not respond successfully."""


class Busy(Exception):
    """The generation slots are in use."""


class TargetChanged(Exception):
    """The device or the project changed after it was selected."""


class DraftExpired(Exception):
    """The reviewed draft expired or was already pushed."""


class GenerationDisabled(Exception):
    """Configuration generation is no longer available."""


@dataclass(frozen=True)
class Draft:
    target: fix_jobs.FixTarget
    owner: str
    commands: tuple[str, ...]
    expires: float


def targets(app: FastAPI) -> list[str]:
    graph = getattr(app.state, "graph", None)
    if (
        graph is None
        or not getattr(app.state, "project_id", None)
        or not getattr(app.state, "gns3_vm_ip", None)
    ):
        return []
    return sorted(
        name
        for name, node in graph.nodes.items()
        if node.env == Environment.ON_GNS3 and _IOSV_IMAGE.fullmatch(node.image)
    )


def _target(app: FastAPI, name: str) -> fix_jobs.FixTarget:
    target = fix_jobs.current_target(app, name)
    if name not in targets(app) or target is None:
        raise TargetChanged("Choose a compatible device in the active GNS3 project.")
    return target


def _context(app: FastAPI, target: fix_jobs.FixTarget) -> dict:
    try:
        ports = {port["name"] for port in list_node_ports(app, target.device)}
    except (requests.RequestException, RuntimeError, ValueError):
        # Linked interfaces from the stored graph are available even if GNS3's port list is not.
        ports = {local for local, _, _ in target.links}
    return {
        "device": target.device,
        "role": target.role,
        "image": target.image,
        "platform": "iosvl2" if _IOSV_IMAGE.fullmatch(target.image)[1] else "iosv",
        "ports": sorted({_canonical_port(port) for port in ports})[:100],
        "links": [
            {"port": _canonical_port(local), "neighbor": peer, "neighbor_port": remote}
            for local, peer, remote in target.links[:100]
        ],
    }


def _generate_settings(prompt: str, context: dict) -> object:
    url = _generator_url()
    if not _SLOTS.acquire(blocking=False):
        raise Busy
    try:
        response = requests.post(
            f"{url}/api/config-draft",
            json={"prompt": prompt, "context": context},
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


def _compile(settings: object, ports: list[str], platform: str) -> list[str]:
    try:
        operations = _OPERATIONS.validate_python(settings)
        commands = ["configure terminal"]
        for operation in operations:
            if isinstance(operation, VlanSettings):
                if platform != "iosvl2":
                    raise InvalidDraft("VLAN definitions need an IOSvL2 switch.")
                commands.extend(
                    (f"vlan {operation.vlan_id}", f"name {operation.name}", "exit")
                )
                continue
            if operation.interface not in ports:
                raise InvalidDraft(
                    f"Interface {operation.interface} is not in the device's port list."
                )
            commands.append(f"interface {operation.interface}")
            if isinstance(operation, DescriptionSettings):
                commands.append(f"description {operation.description}")
            elif isinstance(operation, AccessVlanSettings):
                if platform != "iosvl2":
                    raise InvalidDraft("Access ports need an IOSvL2 switch.")
                commands.extend(
                    (
                        "switchport mode access",
                        f"switchport access vlan {operation.vlan_id}",
                    )
                )
            else:
                if platform != "iosv":
                    raise InvalidDraft(
                        "This draft supports IPv4 addresses on IOSv routed ports."
                    )
                address = IPv4Interface(operation.address)
                commands.append(f"ip address {address.ip} {address.netmask}")
            commands.append("exit")
        return [*commands, "end"]
    except (ValidationError, ValueError) as exc:
        raise InvalidDraft(
            "The reply contains unsupported configuration settings."
        ) from exc


def generate(app: FastAPI, request: GenerateRequest, owner: str) -> dict:
    target = _target(app, request.device)
    context = _context(app, target)
    settings = _generate_settings(request.prompt, context)
    commands = _compile(settings, context["ports"], context["platform"])
    if _target(app, request.device) != target:
        raise TargetChanged("The project or device changed. Generate a new draft.")
    if not get_enabled() or not available():
        raise GenerationDisabled("AI configuration has been disabled.")
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
        drafts[draft_id] = Draft(target, owner, tuple(commands), now + _DRAFT_TTL)
    return {"draft_id": draft_id, "device": request.device, "commands": commands}


def push_draft(app: FastAPI, draft_id: str, owner: str) -> push_jobs.PushJob:
    with _DRAFT_LOCK:
        drafts = getattr(app.state, "ai_snippet_drafts", {})
        draft = drafts.get(draft_id)
        if draft is None or draft.owner != owner or draft.expires <= time.monotonic():
            raise DraftExpired

        def verify_target() -> None:
            if not get_enabled() or not available():
                raise GenerationDisabled("AI configuration has been disabled.")
            if _target(app, draft.target.device) != draft.target:
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
