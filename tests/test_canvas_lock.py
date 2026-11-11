"""The canvas is locked while an import runs, and never stays locked."""

from pathlib import Path

from frontend_js import read_app_js

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def _import_handler() -> str:
    source = read_app_js()
    start = source.index('"import-project-input").addEventListener("change"')
    return source[start : source.index("\n});\n", start)]


def test_the_import_locks_the_canvas_after_the_confirmation_and_unlocks_in_finally():
    handler = _import_handler()
    assert handler.index("showConfirm(") < handler.index("lockCanvas(")
    lock = handler.index("lockCanvas(")
    assert handler.index("try {", lock) < handler.index("} finally {")
    assert "unlockCanvas();" in handler[handler.index("} finally {") :]


def test_keyboard_shortcuts_do_nothing_while_the_canvas_is_locked():
    source = read_app_js()
    shortcuts = source[
        source.index('window.addEventListener("keydown", async (evt) => {') :
    ]
    shortcuts = shortcuts[: shortcuts.index("const mod =")]
    assert "if (canvasLocked) return;" in shortcuts


def test_the_lock_covers_the_canvas_and_blocks_palette_and_device_list():
    html = (STATIC / "index.html").read_text()
    css = (STATIC / "styles.css").read_text()
    canvas_area = html[html.index('<div id="canvas-area">') :]
    assert (
        'id="canvas-lock" hidden'
        in canvas_area[: canvas_area.index('<div id="right-panel-resize-handle"')]
    )
    assert "#canvas-lock[hidden] { display: none; }" in css
    assert "body.canvas-locked #palette-list" in css
    assert "body.canvas-locked #topology-summary-list" in css


def test_the_screenshot_reads_the_linked_style_sheet_not_a_style_element():
    # the page's CSS lives in /styles.css; document.querySelector("style") is null there and broke the screenshot
    js = read_app_js()
    body = js[js.index("async function buildCanvasDiagram") :]
    body = body[: body.index("\n}\n")]
    assert 'document.querySelector("style")' not in body
    assert "await pageCss()" in body
