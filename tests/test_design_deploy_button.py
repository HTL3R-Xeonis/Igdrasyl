"""The one-click Deploy button of the design mode: present, hidden until the mode shows it, no hover text, loaded in order."""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
BUTTON_JS = (STATIC / "design-deploy-button.js").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")


def _tag(element_id: str) -> str:
    match = re.search(rf"<(?:button|span)[^>]*id=\"{element_id}\"[^>]*>", HTML)
    assert match, f"{element_id} is missing from index.html"
    return match.group(0)


def test_the_button_and_its_reason_exist_and_start_hidden_without_a_title():
    for element_id in ("design-deploy-btn", "design-deploy-reason"):
        tag = _tag(element_id)
        assert " hidden" in tag
        assert "title=" not in tag


def test_the_button_sits_right_after_the_mode_badge():
    badge = HTML.index('id="menubar-mode-badge"')
    button = HTML.index('id="design-deploy-btn"')
    stage = HTML.index('id="stage-changes-btn"')
    assert badge < button < stage


def test_the_script_loads_after_the_files_it_builds_on():
    order = re.findall(r'<script src="/([a-z-]+)\.js', HTML)
    for needed in ("import-and-generate", "projects", "user-menu", "drawings"):
        assert order.index(needed) < order.index("design-deploy-button")


def test_the_script_has_no_confirmation_step_and_reuses_the_dialog_calls():
    assert "showConfirm" not in BUTTON_JS and "showAlert" not in BUTTON_JS
    for call in (
        "restartIntoProject",
        "startLibraryDeployment",
        "resumeHeldProject",
        "saveDesignProject(null)",
    ):
        assert call in BUTTON_JS


def test_the_frame_is_an_overlay_that_cannot_catch_clicks_or_shift_the_layout():
    block = CSS[CSS.index("body.design-mode #canvas-area::after") :]
    block = block[: block.index("}")]
    assert "position: absolute" in block and "inset: 0" in block
    assert "pointer-events: none" in block
    assert "--mode-frame" in CSS and "#f2c230" in CSS


def test_the_new_button_adds_no_hover_rule():
    assert "#design-deploy-btn:hover" not in CSS
