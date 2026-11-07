"""The canvas is locked while an import runs, and never stays locked."""

from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def _import_handler() -> str:
    source = (STATIC / "app.js").read_text()
    start = source.index('"import-project-input").addEventListener("change"')
    return source[start : source.index("\n});\n", start)]


def test_the_import_locks_the_canvas_after_the_confirmation_and_unlocks_in_finally():
    handler = _import_handler()
    assert handler.index("showConfirm(") < handler.index("lockCanvas(")
    lock = handler.index("lockCanvas(")
    assert handler.index("try {", lock) < handler.index("} finally {")
    assert "unlockCanvas();" in handler[handler.index("} finally {") :]


def test_keyboard_shortcuts_do_nothing_while_the_canvas_is_locked():
    source = (STATIC / "app.js").read_text()
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
