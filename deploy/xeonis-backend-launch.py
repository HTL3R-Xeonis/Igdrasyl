#!/usr/bin/env python3
"""
Resolves one profile's own port and settings file, then execs
uvicorn for it - the systemd unit template (xeonis-backend@.service)
calls this with the profile stem (systemd's `%i`) as its one argument,
since a plain unit file can't read JSON itself to pick a --port.

Reads TOPOLOGYOPERATOR_BACKEND_PORTS_FILE the same way
app/backend_ports.py does (not by importing it - this script runs
before the venv's own packages are necessarily on sys.path in every
deployment shape, and the file format is a one-line, stable contract:
{"<profile filename>": <port>, ...}).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _profile_filename(stem: str, profiles_dir: Path) -> str:
    for suffix in (".yml", ".yaml"):
        if (profiles_dir / f"{stem}{suffix}").exists():
            return f"{stem}{suffix}"
    sys.exit(f"xeonis-backend-launch: no {stem}.yml or {stem}.yaml in {profiles_dir}")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: xeonis-backend-launch.py <profile-stem>")
    stem = sys.argv[1]

    profiles_dir = Path(
        os.environ.get("TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR", "settings_profiles")
    )
    filename = _profile_filename(stem, profiles_dir)

    ports_file = Path(
        os.environ.get(
            "TOPOLOGYOPERATOR_BACKEND_PORTS_FILE", "settings_profiles_ports.json"
        )
    )
    try:
        ports = json.loads(ports_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"xeonis-backend-launch: can't read {ports_file}: {exc}")
    port = ports.get(filename)
    if port is None:
        sys.exit(
            f"xeonis-backend-launch: {filename!r} has no assigned port in {ports_file} "
            "- the gateway assigns one on first activate, before starting this unit"
        )

    os.environ["TOPOLOGYOPERATOR_SETTINGS"] = str(profiles_dir / filename)
    os.environ["TOPOLOGYOPERATOR_GATEWAY_BACKEND"] = "1"
    uvicorn = str(Path(sys.executable).parent / "uvicorn")
    os.execv(
        uvicorn,
        [uvicorn, "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
    )


if __name__ == "__main__":
    main()
