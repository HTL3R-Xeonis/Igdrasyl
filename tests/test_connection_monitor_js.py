"""Runs the plain-node tests of the "Connection lost" monitor (tests/js) inside pytest when node is installed."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_connection_monitor_logic_passes_its_node_tests():
    result = subprocess.run(
        ["node", "--test", str(ROOT / "tests" / "js")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
