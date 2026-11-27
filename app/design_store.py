"""
Persists app.state.design (see app/design.py) to a small local JSON file
so a process restart - a crash, a redeploy, or a manual retry via
POST /reload - doesn't silently wipe an in-progress sketch, including one
just produced by "Generate via LLM" (see app/generate_jobs.py). Same
treatment as app/layout.py's own local file: per-deployment, gitignored,
never topology truth.

Deliberately never touches the template service: saving/loading round-
trips design.to_dict/from_dict directly, which restore each node's
already-resolved `environment` instead of re-asking
Environment.get_environment - otherwise this would fail to restore a
sketch during exactly the kind of template-service outage it's meant to
survive.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from loguru import logger

from app import design

_DESIGN_FILE_ENV_VAR = "TOPOLOGYOPERATOR_DESIGN_FILE"
_DEFAULT_DESIGN_FILE = "design_state.json"


def _design_path() -> Path:
    return Path(os.getenv(_DESIGN_FILE_ENV_VAR, _DEFAULT_DESIGN_FILE))


_load_problem: str | None = None


def take_load_problem() -> str | None:
    """What went wrong with the last load() (and where the file went), once - for the startup warning; None if nothing."""
    global _load_problem
    problem, _load_problem = _load_problem, None
    return problem


def load() -> design.DesignState | None:
    """
    None if there's no saved sketch, or the file can't be read/parsed - the caller falls back to design.new_state().

    An unreadable file is moved aside (design_state.json.corrupt-<time>) instead of being left where the next save would
    overwrite it: the sketch it holds may still be recoverable by hand. take_load_problem() says so.
    """
    global _load_problem
    path = _design_path()
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return design.from_dict(json.load(f))
    except (OSError, ValueError, KeyError) as exc:
        aside = path.with_name(
            f"{path.name}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
        try:
            path.rename(aside)
            kept = f"kept as {aside.name}"
        except OSError as rename_exc:
            kept = f"could not be moved aside ({rename_exc}) and will be overwritten by the next save"
        _load_problem = f"The saved design sketch {path.name} was unreadable ({exc}); {kept}. Starting with an empty sketch."
        logger.warning(_load_problem)
        return None


def save(state: design.DesignState) -> None:
    path = _design_path()
    with open(path, "w") as f:
        json.dump(design.to_dict(state), f)
