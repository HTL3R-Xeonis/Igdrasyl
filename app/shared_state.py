"""
The install-wide files a gateway and every backend must share. Their relative defaults resolve against the working
directory - fine for one process, but with several each would silently get its own user store, session secret and
profiles. A managed process therefore refuses to start unless all four are set to absolute paths.
"""

from __future__ import annotations

import os

from loguru import logger

SHARED_STATE_VARIABLES = (
    "TOPOLOGYOPERATOR_USERS_FILE",
    "TOPOLOGYOPERATOR_SESSION_SECRET_FILE",
    "TOPOLOGYOPERATOR_AI_CONFIG_FILE",
    "TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR",
)


def _problem(variable: str) -> str:
    return (
        f"{variable} is not set to an absolute path - every gateway and backend unit "
        "must share the same file (see deploy/README.md)."
    )


def require_shared_state_paths() -> None:
    """
    :raises RuntimeError: a variable is missing or not an absolute path. Only names and paths are logged, never contents.
    """
    problems = [
        _problem(name)
        for name in SHARED_STATE_VARIABLES
        if not os.path.isabs(os.getenv(name, ""))
    ]
    if not problems:
        return
    for problem in problems:
        logger.error(problem)
    raise RuntimeError(" ".join(problems))
