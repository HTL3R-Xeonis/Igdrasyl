"""
Reviewed configuration drafts for a single live device.

The generator service answers with the device's own commands (already syntax-checked there). They are
validated here once more, scanned for dangerous commands (advisory warnings, never a block), shown to the
user and only pushed after that review - see README, "Generate with AI".
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, field_validator
from src.graph.environment import Environment
from src.settings import Settings

from app import fix_jobs, guest_nics, push_jobs
from app.config_push import DeviceSpec
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


MAX_PROMPT_LENGTH = 1500


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_LENGTH)

    @field_validator("prompt")
    @classmethod
    def trim_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter a prompt.")
        return value.strip()


# Platforms the generator writes for, and what each one is called in the device list.
PLATFORM_LABELS = {
    "iosv": "Cisco IOSv",
    "iosvl2": "Cisco IOSvL2",
    "linux": "Linux",
    "windows": "Windows",
    "routeros": "RouterOS",
    "vpcs": "VPCS",
    "pfsense": "pfSense/OPNsense",
    "other": "GNS3 node",
}
_CISCO_PLATFORMS = frozenset({"iosv", "iosvl2"})
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

# Limits of a draft (the generator promises the same ones; they are enforced here again).
MAX_COMMANDS = 200
MAX_COMMAND_LENGTH = 500
MAX_DRAFT_BYTES = 20_000
MAX_WARNINGS = 50
MAX_WARNING_LENGTH = 300
_MAX_RAW_ENTRIES = 1000
_MAX_REASON_LENGTH = 500


def _canonical_port(port: str) -> str:
    return _PORT_PREFIX.sub(lambda match: _PORT_NAMES[match[0].lower()], port)


class InvalidDraft(Exception):
    """The generator refused the request, or its reply is not a usable draft; the message is for the user."""


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
    warnings: tuple[str, ...] = ()


def platform_of(node) -> str | None:
    """The platform the generator writes for; None where the device has no config-push transport at all."""
    image = str(getattr(node, "image", "")).lower()
    if node.env == Environment.ON_GNS3:
        match = _IOSV_IMAGE.fullmatch(node.image)
        if match:
            return "iosvl2" if match[1] else "iosv"
        if "vpcs" in image:
            return "vpcs"
        return "other"
    if node.env == Environment.ON_ESXI:
        if "windows" in image:
            return "windows"
        if "mikrotik" in image or "routeros" in image:
            return "routeros"
        if "pfsense" in image or "opnsense" in image:
            return "pfsense"
        return "linux"
    return None


def unsupported_reason(node) -> str:
    return "This device has no configuration push transport."


def _graph_ready_for(app: FastAPI, node) -> bool:
    """GNS3 nodes need the selected project and its GNS3 VM, ESXi VMs only the host they are on."""
    if node.env == Environment.ON_GNS3:
        return bool(
            getattr(app.state, "project_id", None)
            and getattr(app.state, "gns3_vm_ip", None)
        )
    return True


def targets(app: FastAPI) -> list[str]:
    graph = getattr(app.state, "graph", None)
    if graph is None:
        return []
    return sorted(
        name
        for name, node in graph.nodes.items()
        if platform_of(node) is not None and _graph_ready_for(app, node)
    )


def devices(app: FastAPI) -> list[dict]:
    """Every device of the active project with the platform it would be written for, or why it can not be."""
    graph = getattr(app.state, "graph", None)
    if graph is None:
        return []
    result = []
    for name, node in sorted(graph.nodes.items()):
        platform = platform_of(node)
        if platform is None:
            reason = unsupported_reason(node)
        elif not _graph_ready_for(app, node):
            reason = "Select a GNS3 project on the host first."
        else:
            reason = ""
        result.append(
            {
                "name": name,
                "platform": platform,
                "label": PLATFORM_LABELS.get(platform, ""),
                "supported": not reason,
                "reason": reason,
            }
        )
    return result


def _target(app: FastAPI, name: str) -> fix_jobs.FixTarget:
    target = fix_jobs.current_target(app, name)
    if name not in targets(app) or target is None:
        raise TargetChanged("Choose a supported device of the active project.")
    return target


def _context(app: FastAPI, target: fix_jobs.FixTarget) -> dict:
    node = app.state.graph.nodes[target.device]
    platform = platform_of(node)
    try:
        ports = {port["name"] for port in list_node_ports(app, target.device)}
    except (requests.RequestException, RuntimeError, ValueError):
        # Linked interfaces from the stored graph are available even if GNS3's port list is not
        # (and for ESXi VMs, which GNS3 does not know at all).
        ports = {local for local, _, _ in target.links}
    # Only Cisco names are rewritten to their long form; a Linux "eth0" must stay "eth0".
    name_of = _canonical_port if platform in _CISCO_PLATFORMS else str
    return {
        "device": target.device,
        "role": target.role,
        "image": target.image,
        "platform": platform,
        "ports": sorted({name_of(port) for port in ports})[:100],
        "links": [
            {"port": name_of(local), "neighbor": peer, "neighbor_port": remote}
            for local, peer, remote in target.links[:100]
        ],
    }


def _with_guest_names(
    app: FastAPI, context: dict
) -> tuple[dict, list[dict[str, str]], list[str]]:
    """
    For a Linux or Windows VM on ESXi the topology names (eth0, ...) do not exist in the guest: replace the ports and
    links of `context` by the names the guest really has (found through the NIC's MAC, see app/guest_nics.py).
    Returns (context for the generator, [{"topology", "guest"}] for the review box, warnings). Anything else - GNS3
    nodes, appliances reached over serial - is returned unchanged; a failed lookup keeps the topology names and warns.
    """
    if context["platform"] not in {"linux", "windows"}:
        return context, [], []
    node = app.state.graph.nodes[context["device"]]
    if node.env != Environment.ON_ESXI:
        return context, [], []
    wanted = {*context["ports"], *(link["port"] for link in context["links"])}
    found = guest_nics.resolve(app, node, sorted(wanted))
    if not found.names:
        return context, [], found.warnings
    guest = found.names
    renamed = {
        **context,
        "ports": sorted({guest.get(port, port) for port in context["ports"]}),
        "links": [
            {**link, "port": guest.get(link["port"], link["port"])}
            for link in context["links"]
        ],
    }
    interfaces = [
        {"topology": topology, "guest": name}
        for topology, name in sorted(guest.items())
    ]
    # guest name -> the name the topology (and so the user's request) uses; sent only when there is one
    aliases = {
        name: topology for topology, name in sorted(guest.items()) if name != topology
    }
    if aliases:
        renamed["interface_aliases"] = aliases
    return renamed, interfaces, found.warnings


def rewrite_prompt(prompt: str, interfaces: list[dict[str, str]]) -> str:
    """
    The prompt with every topology interface name that was resolved replaced by the guest's name, so the request
    and the context's ports speak of the same interface. One pass with one pattern (longest names first), never
    one name after the other: eth2 -> ens160 must not be hit again by a later replacement. Only whole tokens
    count - not inside words ("method0"), paths, host names or addresses ("10.0.0.eth0") - and case is ignored.
    A VLAN suffix stays: "eth0.30" becomes "<guest>.30".
    """
    mapping = {
        item["topology"].lower(): item["guest"]
        for item in interfaces
        if item["topology"] != item["guest"]
    }
    if not mapping:
        return prompt
    names = sorted(mapping, key=len, reverse=True)
    pattern = re.compile(
        r"(?<![\w.\-/])(?:"
        + "|".join(re.escape(name) for name in names)
        + r")(?![\w/]|-\w)",
        re.IGNORECASE,
    )
    return pattern.sub(lambda match: mapping[match[0].lower()], prompt)


def _plain(text: str, limit: int) -> str:
    """Text for the user: control and formatting characters become spaces, whitespace is collapsed, then cut to `limit`."""
    cleaned = "".join(
        " " if unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} else char
        for char in text
    )
    return " ".join(cleaned.split())[:limit]


def _refusal_reason(response: requests.Response) -> str:
    """The generator's own reason of a 422, passed on to the user as it is (FastAPI validation lists are joined)."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, list):
        detail = " ".join(
            str(item.get("msg", "")) if isinstance(item, dict) else str(item)
            for item in detail
        )
    reason = _plain(detail, _MAX_REASON_LENGTH) if isinstance(detail, str) else ""
    return reason or "The generator refused this request."


def _rejects_aliases(response: requests.Response) -> bool:
    """True if a 422 is FastAPI's "extra input not permitted" for context.interface_aliases (an older generator)."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return False
    return isinstance(detail, list) and any(
        isinstance(item, dict)
        and "interface_aliases" in [str(part) for part in item.get("loc") or []]
        and "extra" in str(item.get("type", ""))
        for item in detail
    )


def _request_draft(prompt: str, context: dict) -> object:
    url = _generator_url()
    if not _SLOTS.acquire(blocking=False):
        raise Busy

    def post(body_context: dict) -> requests.Response:
        return requests.post(
            f"{url}/api/config-draft",
            json={"prompt": prompt, "context": body_context},
            headers={"Authorization": f"Bearer {service_token()}"},
            timeout=min(90, Settings.API.TOPOLOGY_GENERATOR_TIMEOUT_SECONDS),
            allow_redirects=False,
        )

    try:
        response = post(context)
        if (
            response.status_code == 422
            and "interface_aliases" in context
            and _rejects_aliases(response)
        ):
            # A generator from before the aliases refuses the unknown field: ask again without it.
            response = post(
                {k: v for k, v in context.items() if k != "interface_aliases"}
            )
        if response.status_code == 422:
            raise InvalidDraft(_refusal_reason(response))
        if response.status_code != 200:
            raise GeneratorUnavailable
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        raise GeneratorUnavailable from exc
    finally:
        _SLOTS.release()


_ENTER_CONFIG_MODE = re.compile(
    r"^conf(?:ig(?:ure)?)?(?:\s+t(?:erminal)?)?$", re.IGNORECASE
)


def _check_command(number: int, command: str) -> str:
    if "```" in command:
        raise InvalidDraft(f"Command {number} contains a markdown code fence.")
    if len(command) > MAX_COMMAND_LENGTH:
        raise InvalidDraft(
            f"Command {number} is longer than {MAX_COMMAND_LENGTH} characters."
        )
    for char in command:
        if char != "\t" and unicodedata.category(char) in {
            "Cc",
            "Cf",
            "Cs",
            "Zl",
            "Zp",
        }:
            raise InvalidDraft(
                f"Command {number} contains a control character or a line break."
            )
    return command


def validate_reply(reply: object, platform: str | None) -> tuple[list[str], list[str]]:
    """
    (commands, the generator's warnings) from the generator's 200 answer - checked, not interpreted:
    any command is allowed, but it must be one plain line (no control characters, no markdown fence),
    the list must be 1..MAX_COMMANDS lines of at most MAX_COMMAND_LENGTH characters and MAX_DRAFT_BYTES in
    total. Blank lines are dropped. A Cisco draft may not enter or leave configuration mode itself: the
    push does that (see DeviceSpec.native_shell).
    :raises InvalidDraft: with the reason, for the user
    """
    raw_warnings = reply.get("warnings") if isinstance(reply, dict) else None
    if (
        not isinstance(reply, dict)
        or not isinstance(reply.get("commands"), list)
        or not isinstance(raw_warnings or [], list)
    ):
        raise InvalidDraft("The generator's reply is not a list of commands.")
    raw = reply["commands"]
    if len(raw) > _MAX_RAW_ENTRIES:
        raise InvalidDraft(f"The draft has more than {MAX_COMMANDS} commands.")
    commands: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise InvalidDraft(
                "The generator's reply contains a command that is not text."
            )
        line = item.strip()
        if line:
            commands.append(_check_command(len(commands) + 1, line))
    if platform in _CISCO_PLATFORMS:
        while commands and _ENTER_CONFIG_MODE.match(commands[0]):
            commands.pop(0)
        while commands and commands[-1].lower() == "end":
            commands.pop()
    if not commands:
        raise InvalidDraft("The generator returned no commands for this request.")
    if len(commands) > MAX_COMMANDS:
        raise InvalidDraft(f"The draft has more than {MAX_COMMANDS} commands.")
    if sum(len(command.encode("utf-8")) + 1 for command in commands) > MAX_DRAFT_BYTES:
        raise InvalidDraft(f"The draft is larger than {MAX_DRAFT_BYTES} bytes.")
    warnings = [
        text
        for text in (
            _plain(warning, MAX_WARNING_LENGTH)
            for warning in raw_warnings or []
            if isinstance(warning, str)
        )
        if text
    ]
    return commands, warnings


# A command may start with sudo (Linux) or do (Cisco, from configuration mode).
_START = r"^\s*(?:sudo\s+|do\s+)?"


def _rule(*alternatives: str) -> re.Pattern[str]:
    return re.compile("|".join(alternatives))


_RESTART = _rule(
    _START + r"(?:reload|reboot|halt|poweroff)\b",
    _START + r"(?:init\s+[06]\b|shutdown\s+\S|systemctl\s+(?:reboot|poweroff|halt)\b)",
    _START + r"(?:(?:restart|stop)-computer\b|/system\s+(?:reboot|shutdown)\b)",
    r"^\s*@(?:reboot|wait-reboot)\b",
)
_ERASE = _rule(
    r"\bwrite\s+erase\b",
    _START + r"(?:erase|format|mkfs(?:\.\w+)?|wipefs|fdisk|parted|shred)\b",
    _START + r"(?:diskpart|format-volume|clear-disk)\b",
    _START + r"delete\s+(?:/\S+\s+)*(?:flash|nvram|vlan)",
    _START + r"dd\s+.*\bof=/dev/",
    _START + r"/system\s+reset-configuration\b",
    r"\bfactory-?reset\b",
)
_RECURSIVE_DELETE = _rule(
    _START + r"rm\s+(?:-\S*r\S*|--recursive)\b",
    _START + r"(?:rd|rmdir)\s+/s\b",
    _START + r"del\s+.*/s\b",
    r"\bremove-item\b.*-recurse",
)
_LINK_DOWN = _rule(
    r"^\s*shutdown\s*$",
    _START + r"ip\s+link\s+(?:set\s+(?:dev\s+)?\S+\s+down|delete)\b",
    _START + r"(?:ifdown|disable-netadapter)\b",
    _START + r"(?:ifconfig\s+\S+\s+down|nmcli\s+.*\b(?:down|disconnect))\b",
    _START + r"systemctl\s+(?:stop|disable)\s+(?:network\S*|systemd-networkd|sshd?)\b",
    r"\bnetsh\s+interface\s+set\s+interface\b.*\bdisable",
    r"^\s*/interface\s+(?:\S+\s+)?disable\b",
)
_FIREWALL = _rule(
    r"\biptables\b.*(?:\s-f\b|--flush)",
    r"\bnft\s+flush\b",
    r"\bufw\s+(?:disable|reset)\b",
    r"\bset-netfirewallprofile\b.*-enabled\s+false",
    r"\badvfirewall\s+set\b.*\bstate\s+off",
)
# a route or gateway command that names the default route
_DEFAULT_ROUTE = _rule(
    r"(?=.*(?:\broute\b|\bgateway\b|\bgw\b))"
    r"(?=.*(?:\bdefault\b|\b0\.0\.0\.0(?:/0|\s+(?:mask\s+)?0\.0\.0\.0)|::/0))"
)
_CREDENTIALS = _rule(
    r"\b(?:password|passwd|secret|snmp-server\s+community|key-string|private-key)\b",
    r"\b(?:pre-shared-key|psk|chpasswd|convertto-securestring|authorized_keys)\b",
    r"\b(?:username|useradd|usermod|new-localuser|set-localuser)\b",
    r"\bnet\s+user\b",
    r"^\s*/user\s+(?:add|set)\b",
)
# (pattern on the lower-cased command, what the command does, whether the warning quotes it)
_ADVISORIES: tuple[tuple[re.Pattern[str], str, bool], ...] = (
    (_RESTART, "restarts or powers off the device", True),
    (_ERASE, "erases or formats data (configuration, files or disks)", True),
    (_RECURSIVE_DELETE, "deletes files recursively", True),
    (
        _LINK_DOWN,
        "shuts down an interface or a network service (the connection may drop)",
        True,
    ),
    (_FIREWALL, "changes or disables the firewall", True),
    (_DEFAULT_ROUTE, "changes the default route", True),
    (
        _CREDENTIALS,
        "sets or changes credentials (the command is not repeated here)",
        False,
    ),
)


def advisory_warnings(commands: list[str]) -> list[str]:
    """
    Warnings of this program for commands that deserve a second look. Purely advisory: a flagged command
    stays in the draft, the reviewer decides. The scan is a short list of patterns, not a safety proof.
    """
    found: list[str] = []
    for number, command in enumerate(commands, 1):
        lowered = command.lower()
        for pattern, text, quote in _ADVISORIES:
            if pattern.search(lowered):
                shown = _plain(command, 80) if quote else ""
                found.append(
                    f"Command {number} {text}" + (f": {shown}" if shown else ".")
                )
    return found


def all_warnings(generator_warnings: list[str], commands: list[str]) -> list[str]:
    """The generator's warnings first, then this program's own, without repeats."""
    merged: list[str] = []
    for warning in [*generator_warnings, *advisory_warnings(commands)]:
        if warning not in merged:
            merged.append(warning)
    return merged[:MAX_WARNINGS]


def generate(app: FastAPI, request: GenerateRequest, owner: str) -> dict:
    target = _target(app, request.device)
    context, interfaces, name_warnings = _with_guest_names(app, _context(app, target))
    prompt = rewrite_prompt(request.prompt, interfaces)
    if len(prompt) > MAX_PROMPT_LENGTH:
        raise InvalidDraft(
            f"The prompt is longer than {MAX_PROMPT_LENGTH} characters once the interface names are "
            "written as the guest has them. Shorten it."
        )
    commands, generator_warnings = validate_reply(
        _request_draft(prompt, context), context["platform"]
    )
    if _target(app, request.device) != target:
        raise TargetChanged("The project or device changed. Generate a new draft.")
    if not get_enabled() or not available():
        raise GenerationDisabled("AI configuration has been disabled.")
    warnings = all_warnings([*name_warnings, *generator_warnings], commands)
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
            target, owner, tuple(commands), now + _DRAFT_TTL, tuple(warnings)
        )
    return {
        "draft_id": draft_id,
        "device": request.device,
        "platform": context["platform"],
        "commands": commands,
        "warnings": warnings,
        "interfaces": interfaces,
        "prompt_used": prompt,
    }


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
            # native_shell: the push puts the commands where the device expects them (IOS
            # configuration mode, PowerShell on Windows) - the same path a manual push takes.
            {
                draft.target.device: DeviceSpec(
                    commands=list(draft.commands), native_shell=True
                )
            },
            wait_seconds=0,
            audit_note="AI snippet",
            verify_target=verify_target,
        )
        del drafts[draft_id]
        return job
