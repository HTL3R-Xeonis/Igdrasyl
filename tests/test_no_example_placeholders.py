"""Input placeholders describe the field; they never suggest example values ("e.g. ...", "Example: ...")."""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
PLACEHOLDER = re.compile(
    r"""placeholder\s*[=:]\s*(?P<q>["'])(?P<text>(?:\\.|(?!(?P=q)).)*)(?P=q)""",
    re.IGNORECASE,
)
EXAMPLE_WORDS = re.compile(
    r"\b(e\.g\.|example|for example|for instance|z\.\s?b\.)", re.IGNORECASE
)


def _placeholders():
    for path in sorted([*STATIC.glob("*.html"), *STATIC.glob("*.js")]):
        for match in PLACEHOLDER.finditer(path.read_text(encoding="utf-8")):
            yield path.name, match.group("text")


def test_the_scan_finds_the_placeholders_it_is_meant_to_check():
    texts = [text for _, text in _placeholders()]
    assert len(texts) > 10 and any("Find node" in text for text in texts)


def test_no_placeholder_suggests_an_example_value():
    offenders = [
        (name, text) for name, text in _placeholders() if EXAMPLE_WORDS.search(text)
    ]
    assert offenders == [], (
        f"placeholders must not contain example suggestions: {offenders}"
    )
