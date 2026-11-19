"""Admin panel: have the Topology Generator summarize the current log."""

from __future__ import annotations

import requests
from src.settings import Settings

from app import log_store, public_text

MAX_LINES = 2000


def recent_log_text(lines: int = MAX_LINES) -> str:
    """The most recent `lines` log lines, redacted, joined as plain text."""
    return "\n".join(
        public_text.redact_log_text(line)
        for line in log_store.read_lines(Settings.LOG_FILE_PATH, lines)
    )


def summarize(text: str) -> str:
    """
    :raises TimeoutError: the request took too long.
    :raises RuntimeError: the request failed, or the generator doesn't
        support this, or its reply wasn't a usable summary.
    """
    url = Settings.API.TOPOLOGY_GENERATOR_URL.rstrip("/")
    try:
        response = requests.post(
            f"{url}/api/summarize-text",
            json={
                "text": text,
                "goal": "Summarize this Igdrasyl log for an operator - "
                "call out errors and warnings first.",
            },
            timeout=Settings.API.TOPOLOGY_GENERATOR_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        if response.status_code == 404:
            raise RuntimeError(
                "The configured Topology Generator does not support log summaries."
            )
        response.raise_for_status()
    except requests.Timeout as exc:
        raise TimeoutError("Log summary request timed out.") from exc
    except requests.RequestException as exc:
        raise RuntimeError(
            "Log summary request to the Topology Generator failed."
        ) from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError(
            "The Topology Generator returned an invalid response."
        ) from exc
    summary = body.get("summary") if isinstance(body, dict) else None
    if not isinstance(summary, str) or not summary:
        raise RuntimeError("The Topology Generator returned an invalid response.")
    return summary
