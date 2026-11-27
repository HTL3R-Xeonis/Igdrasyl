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


# profile key under `esxi:` - there is no environment variable for it, and TopologyBuilder's built-in default is this lab's own host
# (10.20.20.200): a profile that leaves the address out would silently work on a machine nobody named.
ESXI_SETTINGS = ("ip",)


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


def missing_settings(profile_path: str | Path) -> list[str]:
    """Every required setting the profile (or, for the `api.*` ones, the environment) leaves out, as dotted names."""
    try:
        data = yaml.safe_load(Path(profile_path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []  # reported by the normal settings loading / check_activatable, not here
    esxi = data.get("esxi") if isinstance(data, dict) else None
    esxi = esxi if isinstance(esxi, dict) else {}
    missing = [f"esxi.{key}" for key in ESXI_SETTINGS if not esxi.get(key)]
    return missing + [f"api.{key}" for key in missing_api_settings(profile_path)]


def explain(profile_name: str, missing: list[str]) -> str:
    """:param missing: dotted names (`esxi.ip`, `api.ova_base_url`); a bare `api` key name is accepted as before."""
    keys = [key if "." in key else f"api.{key}" for key in missing]
    env_hint = (
        " (nor the matching environment variables)"
        if any(key.startswith("api.") for key in keys)
        else ""
    )
    return (
        f"Host profile {profile_name!r} does not set {', '.join(keys)}{env_hint} - "
        f"add {'it' if len(keys) == 1 else 'them'} to the profile; the built-in lab addresses are not used as a fallback."
    )


def check(profile_path: str | Path) -> None:
    """:raises MissingProfileSettings: with the text to show the user."""
    missing = missing_settings(profile_path)
    if missing:
        raise MissingProfileSettings(explain(Path(profile_path).name, missing))


def check_activatable(profile_path: str | Path) -> None:
    """
    Everything that would stop the process from coming back up in this profile, named before it is restarted into it: the
    restart replaces the running server, so a profile that cannot start would leave the user with a dead page and the reason
    only in a log.
    :raises MissingProfileSettings: with the text to show the user.
    """
    path = Path(profile_path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MissingProfileSettings(
            f"Host profile {path.name!r} cannot be read: {exc}"
        ) from exc
    except yaml.YAMLError as exc:
        raise MissingProfileSettings(
            f"Host profile {path.name!r} is not valid YAML: {exc}"
        ) from exc
    if data is not None and not isinstance(data, dict):
        raise MissingProfileSettings(
            f"Host profile {path.name!r} must be a YAML mapping of settings, not {type(data).__name__}"
        )
    check(path)
