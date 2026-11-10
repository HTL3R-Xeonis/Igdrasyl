"""
The frontend's main script used to be one app.js; it is now several files that index.html loads in a fixed order and that
share one global scope. Tests that look at "the main script" read all of them, concatenated in that load order, so a marker,
a function or its position relative to another one is found exactly as before.
"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
# not part of the former app.js: loaded after it, with its own version marker
_SEPARATE = {"projects.js"}


def app_js_files() -> list[str]:
    """The local scripts of index.html in load order (xterm from the CDN and projects.js are not part of it)."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    names = re.findall(r'<script src="/([A-Za-z0-9_.-]+\.js)\?v=[^"]*"', html)
    return [name for name in names if name not in _SEPARATE]


def read_app_js() -> str:
    return "\n".join(
        (STATIC / name).read_text(encoding="utf-8") for name in app_js_files()
    )
