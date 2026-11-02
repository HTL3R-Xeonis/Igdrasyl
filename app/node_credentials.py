"""
Default logins of a node, read from the template service it was made from.

An ESXi template (the OVA list, `:8000`) carries `users` and `passwords`; a GNS3 template (`:8001`) usually puts the
default login into its free-text `usage`. This only reads what the template service already serves - nothing is
stored, and nothing is logged. Never raises: when the service cannot be reached the answer says so and the note
dialog still works.
"""

from __future__ import annotations

from typing import Any

import requests
from loguru import logger
from src.connections.api_handler import APIHandler
from src.graph.environment import normalize_template_name
from src.settings import Settings

_NONE: dict[str, Any] = {"source": None, "template": None, "logins": [], "usage": None}


def _logins(template: dict[str, Any]) -> list[dict[str, str | None]]:
    users = [u for u in template.get("users") or [] if isinstance(u, str)]
    passwords = [p for p in template.get("passwords") or [] if isinstance(p, str)]
    if not users:
        return [{"user": None, "password": p} for p in passwords]
    return [
        {"user": user, "password": passwords[i] if i < len(passwords) else None}
        for i, user in enumerate(users)
    ]


def for_image(image: str, environment: str) -> dict[str, Any]:
    """
    {"source": "esxi" | "gns3" | None, "template": name, "logins": [{"user", "password"}], "usage": text | None}
    for the template named `image`; `environment` is the node's ("ON_ESXI" picks the ESXi list, anything else GNS3).
    A template without that information gives empty logins, an unreachable service adds a short "error".
    """
    if Settings.API.LITERAL_API_VALUES:
        return dict(_NONE)
    esxi = environment == "ON_ESXI"
    url = (
        Settings.API.ESXI_TEMPLATE_SERVER_URL
        if esxi
        else Settings.API.GNS3_TEMPLATE_SERVER_URL
    )
    try:
        templates = APIHandler.get(f"{url}/api/templates").get("templates", [])
    except (requests.exceptions.RequestException, TimeoutError, ValueError) as exc:
        logger.warning(f"Couldn't read the template list for a node's login: {exc!r}")
        return {**_NONE, "error": "the template service could not be reached"}
    wanted = normalize_template_name(image)
    for template in templates:
        if normalize_template_name(str(template.get("name", ""))) != wanted:
            continue
        usage = template.get("usage")
        return {
            "source": "esxi" if esxi else "gns3",
            "template": template.get("name"),
            "logins": _logins(template) if esxi else [],
            "usage": usage.strip()
            if isinstance(usage, str) and usage.strip()
            else None,
        }
    return dict(_NONE)
