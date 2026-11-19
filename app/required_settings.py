"""
What a host profile must say itself. TopologyBuilder's Settings carries this lab's addresses as built-in defaults (it began
as a command line tool for one lab); an Operator profile that leaves a service address out would silently talk to them.
A profile (or the environment, which is how the appliance points everything somewhere else) has to name every service.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

# profile key under `api:` -> environment variable TopologyBuilder reads for the same setting
API_SETTINGS = (
    ("esxi_template_server_url", "ESXI_TEMPLATE_SERVER_URL"),
    ("gns3_template_server_url", "GNS3_TEMPLATE_SERVER_URL"),
    ("topology_generator_url", "TOPOLOGY_GENERATOR_URL"),
    ("ova_base_url", "OVA_BASE_URL"),
)


class MissingProfileSettings(RuntimeError):
    pass


def missing_api_settings(profile_path: str | Path) -> list[str]:
    """The `api.*` keys neither the profile nor the environment sets (empty when everything is named)."""
    try:
        data = yaml.safe_load(Path(profile_path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []  # an unreadable or invalid file is reported by the normal settings loading, not here
    api = data.get("api") if isinstance(data, dict) else None
    api = api if isinstance(api, dict) else {}
    return [
        key
        for key, env_name in API_SETTINGS
        if not api.get(key) and not os.getenv(env_name)
    ]


def explain(profile_name: str, missing: list[str]) -> str:
    keys = ", ".join(f"api.{key}" for key in missing)
    return (
        f"Host profile {profile_name!r} does not set {keys} (nor the matching environment variables) - "
        f"add {'it' if len(missing) == 1 else 'them'} to the profile; the built-in lab addresses are not used as a fallback."
    )


def check(profile_path: str | Path) -> None:
    """:raises MissingProfileSettings: with the text to show the user."""
    missing = missing_api_settings(profile_path)
    if missing:
        raise MissingProfileSettings(explain(Path(profile_path).name, missing))
