"""Explanatory text lives behind an "i" icon (app/static/info-popover.js), never as a permanent paragraph."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
MAX_WORDS = 12  # a permanent line longer than this must be an info popover (or a status line with an id)
VOID = {
    "br",
    "hr",
    "img",
    "input",
    "meta",
    "link",
    "source",
    "area",
    "base",
    "col",
    "embed",
    "param",
    "track",
    "wbr",
}


class Scan(HTMLParser):
    """Collects the long permanent <p> texts and checks the info-icon markup rules."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, dict[str, str | None]]] = []
        self.paragraphs: list[tuple[str, int]] = []
        self.current: list[str] | None = None
        self.current_attrs: dict[str, str | None] = {}
        self.problems: list[str] = []
        self.landing_depth: int | None = None
        self.pending_button: bool = False

    def _in_popover(self) -> bool:
        return any(
            "info-popover" in (attrs.get("class") or "").split()
            for _, attrs in self.stack
        )

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if self.pending_button and not ("info-popover" in classes):
            self.problems.append("an .info-btn is not followed by its .info-popover")
        self.pending_button = False
        if "info-btn" in classes:
            if not attrs.get("aria-label"):
                self.problems.append("an .info-btn has no aria-label")
            if "title" in attrs:
                self.problems.append("an .info-btn carries a native title tooltip")
            self.pending_button = True
        if attrs.get("id") == "landing-view" and self.landing_depth is None:
            self.landing_depth = len(self.stack)
        if self.landing_depth is not None:
            if "info-btn" in classes:
                self.problems.append(
                    "an info icon inside #landing-view (the landing page has no hover effects)"
                )
            if "title" in attrs:
                self.problems.append(
                    f"a native title tooltip inside #landing-view on <{tag}>"
                )
        if tag == "p" and not self._in_popover():
            self.current = []
            self.current_attrs = attrs
        if tag not in VOID:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        if tag == "button":
            pass
        if tag == "p" and self.current is not None:
            words = len(" ".join(self.current).split())
            if words > MAX_WORDS and not self.current_attrs.get("id"):
                self.paragraphs.append(
                    (" ".join(" ".join(self.current).split())[:60], words)
                )
            self.current = None
        while self.stack:
            top, _ = self.stack.pop()
            if top == tag:
                break
        if self.landing_depth is not None and len(self.stack) <= self.landing_depth:
            self.landing_depth = None

    def handle_data(self, data):
        if self.current is not None and not self._in_popover():
            self.current.append(data)


def scan(html: str) -> Scan:
    parser = Scan()
    parser.feed(html)
    return parser


def _pages() -> dict[str, str]:
    from app import routes_gateway

    pages = {
        name: (STATIC / name).read_text(encoding="utf-8")
        for name in ("index.html", "admin.html", "logs.html")
    }
    pages["pick-backend"] = routes_gateway._pick_backend_page(("alice", "user"))
    return pages


def test_no_long_permanent_explanatory_paragraph():
    offenders = {
        name: parser.paragraphs
        for name, html in _pages().items()
        if (parser := scan(html)).paragraphs
    }
    assert offenders == {}, (
        f"move these texts behind an info icon (makeInfoButton / .info-row): {offenders}"
    )


def test_info_icon_markup_rules_hold_on_every_page():
    problems = {
        name: parser.problems
        for name, html in _pages().items()
        if (parser := scan(html)).problems
    }
    assert problems == {}


def test_the_scan_really_sees_long_paragraphs_and_popovers():
    long_text = "word " * (MAX_WORDS + 1)
    assert scan(f"<p>{long_text}</p>").paragraphs
    assert not scan(f'<p id="status">{long_text}</p>').paragraphs
    assert not scan(
        f'<p>short <span class="info-popover">{long_text}</span></p>'
    ).paragraphs
    assert scan(
        '<div id="landing-view"><button class="info-btn" aria-label="x">i</button></div>'
    ).problems


def test_the_behavior_file_closes_on_escape_and_builds_icons_for_javascript():
    source = (STATIC / "info-popover.js").read_text(encoding="utf-8")
    assert '"Escape"' in source
    assert "function makeInfoButton" in source
    assert "aria-expanded" in source
    assert (STATIC / "info-popover.js").exists()
