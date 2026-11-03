"""
Dragging a console/screen tab out of the panel opens that session in its own window. The decision "is this a drag, and is it
far enough outside the panel" is pure geometry (app.js, the block between the "tab drag-out" markers) and is run here under
node, skipped where node is not installed; the wiring is checked as text.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = (
    Path(__file__).resolve().parent.parent / "app" / "static" / "app.js"
).read_text(encoding="utf-8")
CSS = (
    Path(__file__).resolve().parent.parent / "app" / "static" / "styles.css"
).read_text(encoding="utf-8")

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)

AREA = {"left": 700, "right": 1000, "top": 400, "bottom": 800}


def _state(start, point, area=AREA):
    block = APP_JS[
        APP_JS.index("// ---- tab drag-out") : APP_JS.index(
            "// ---- end tab drag-out ----"
        )
    ]
    script = f"{block}\nconsole.log(JSON.stringify(tabDragState({json.dumps(start)}, {json.dumps(point)}, {json.dumps(area)})));"
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True, timeout=30
    )
    return json.loads(out.stdout)


@needs_node
def test_a_small_movement_is_a_click_not_a_drag():
    assert _state({"x": 800, "y": 420}, {"x": 804, "y": 423}) == {
        "dragging": False,
        "outside": False,
    }


@needs_node
def test_a_drag_inside_the_panel_does_not_pop_out():
    assert _state({"x": 800, "y": 420}, {"x": 900, "y": 600}) == {
        "dragging": True,
        "outside": False,
    }


@needs_node
@pytest.mark.parametrize(
    "point",
    [
        {"x": 300, "y": 600},  # onto the canvas, left of the panel
        {"x": 1200, "y": 600},  # to the right of it
        {"x": 850, "y": 100},  # above
        {"x": 850, "y": 900},  # below
        {"x": -50, "y": 600},  # out of the browser window altogether
    ],
)
def test_releasing_well_outside_the_panel_pops_out(point):
    assert _state({"x": 800, "y": 420}, point) == {"dragging": True, "outside": True}


@needs_node
def test_a_little_beyond_the_edge_is_still_inside_the_margin():
    assert (
        _state({"x": 800, "y": 420}, {"x": 690, "y": 500})["outside"] is False
    )  # 10 px left of the panel
    assert (
        _state({"x": 800, "y": 420}, {"x": 660, "y": 500})["outside"] is True
    )  # 40 px left of it


def test_the_tab_wiring_is_in_place():
    assert (
        'tab.addEventListener("pointerdown", (evt) => startTabDrag(evt, id));' in APP_JS
    )
    assert "function popOutSession(" in APP_JS and "function restoreSession(" in APP_JS
    assert (
        "session.popout.close()" not in APP_JS.split("function closeSession")[0]
    )  # only closeSession closes a window
    # the click that ends a drag must not switch tabs, and a popped tab focuses its window
    assert (
        "if (suppressTabClick) return;" in APP_JS
        and "session.popout.focus();" in APP_JS
    )
    # moving the live panel, not a copy: the same element is appended to the new window
    assert "panels.appendChild(session.panelEl);" in APP_JS


def test_closing_the_window_brings_the_session_back_and_closing_the_session_closes_the_window():
    assert (
        'win.addEventListener("beforeunload", () => restoreSession(session, win));'
        in APP_JS
    )
    body = APP_JS[
        APP_JS.index("function closeSession") : APP_JS.index("// ---- tab drag-out")
    ]
    assert "popout.close()" in body


def test_the_popped_tab_is_marked_and_the_drag_hint_is_styled():
    assert ".session-tab.popped" in CSS and "#tab-dragout-hint" in CSS
    assert (
        "touch-action: none" in CSS
    )  # a touch drag on a tab must not scroll the page instead


def test_the_window_title_is_set_after_the_head_is_copied():
    # copying the head also copies this page's own <title>, which would otherwise win
    for marker in ("session.title", '"Ginnungagap - Console/Screen View"'):
        at = APP_JS.index(marker)
        before = APP_JS[max(0, at - 220) : at]
        assert "head.innerHTML = document.head.innerHTML" in before, marker


def _open_pop_out(as_tab):
    """Runs app.js's openPopOut under node with a window.open that records its arguments."""
    start = APP_JS.index("function openPopOut")
    end = APP_JS.index("function popOutSession")
    script = (
        "const calls = [];\n"
        "const window = { open: (...args) => { calls.push(args); return {}; } };\n"
        + APP_JS[start:end]
        + f"\nopenPopOut({json.dumps(as_tab)}, 'width=900,height=650');\nconsole.log(JSON.stringify(calls));"
    )
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


@needs_node
def test_a_pop_out_is_a_window_unless_a_browser_tab_is_asked_for():
    assert _open_pop_out(False) == [["", "_blank", "width=900,height=650"]]
    # no window features at all: that is how a browser is told to open a tab
    assert _open_pop_out(True) == [["", "_blank"]]


def test_shift_chooses_the_tab_for_both_ways_to_pop_out():
    assert "popOutSession(id, e.screenX, e.screenY, e.shiftKey)" in APP_JS
    assert 'openPopOut(evt.shiftKey, "width=900,height=650")' in APP_JS
    html = (
        Path(__file__).resolve().parent.parent / "app" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    assert "Shift-click: a new browser tab" in html


# ---------- the whole Console/Screen view popped out: only its session parts move ----------


def test_the_session_parts_are_looked_up_through_consoleEl_never_in_this_page_directly():
    # once they live in the pop-out's document, document.getElementById(...) finds nothing there and the code throws
    # (that made loadCanvas fail and the status bar say "Disconnected" while the pop-out was open)
    for name in (
        "session-tabbar",
        "session-tabrow",
        "session-panels",
        "console-placeholder",
    ):
        direct = f'document.getElementById("{name}")'
        uses = [line for line in APP_JS.splitlines() if direct in line]
        assert all(
            "POP_OUT_PARTS" in line or "appendChild(document" in line for line in uses
        ), (name, uses)
    assert (
        'consoleEl("session-panels")' in APP_JS
        and 'consoleEl("console-placeholder")' in APP_JS
    )


def test_the_context_menus_are_not_moved_into_the_pop_out():
    assert (
        'const POP_OUT_PARTS = ["session-tabrow", "console-placeholder", "session-panels"];'
        in APP_JS
    )
    handler = APP_JS[APP_JS.index("function popInPanel") :]
    handler = handler[: handler.index("showLandingView();")]
    assert 'getElementById("console-area")' in handler  # only to put the parts back
    assert "appendChild(consoleArea)" not in handler
    for menu in ("node-actions", "multi-actions", "link-actions"):
        assert (
            menu not in handler
        )  # they are position:fixed popups of the main page and stay in it


def test_the_side_panel_keeps_only_the_device_list_while_popped_out():
    assert (
        'classList.add("popped-out")' in APP_JS
        and 'classList.remove("popped-out")' in APP_JS
    )
    assert (
        "console-area-placeholder" not in APP_JS
    )  # no note in the panel, just the list
    for rule in (
        "#right-panel.popped-out #panel-resize-handle { display: none; }",
        "#right-panel.popped-out #topology-summary { flex: 1 1 auto; height: auto; border-bottom: 0; }",
    ):
        assert rule in CSS


def _reorder(x, rects, start):
    block = APP_JS[
        APP_JS.index("// ---- tab drag-out") : APP_JS.index(
            "// ---- end tab drag-out ----"
        )
    ]
    script = (
        f"{block}\nconsole.log(tabReorderIndex({x}, {json.dumps(rects)}, {start}));"
    )
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True, timeout=30
    )
    return int(out.stdout)


TABS = [
    {"left": 0, "right": 100},
    {"left": 100, "right": 200},
    {"left": 200, "right": 300},
]


@needs_node
@pytest.mark.parametrize(
    ("x", "start", "expected"),
    [
        (150, 1, 1),  # on its own place
        (40, 1, 0),  # before the middle of the tab on its left
        (60, 1, 1),  # not yet past that middle
        (260, 1, 2),  # past the middle of the tab on its right
        (240, 1, 1),
        (290, 0, 2),  # across two tabs at once
        (5, 2, 0),
    ],
)
def test_a_tab_takes_the_place_its_pointer_is_at(x, start, expected):
    assert _reorder(x, TABS, start) == expected


def test_the_tab_row_can_be_reordered_and_a_session_can_be_popped_back_in():
    assert "reorder(e.clientX)" in APP_JS
    # the pop-out window gets the events of its own tabs
    assert "tab.ownerDocument.defaultView" in APP_JS
    assert 'popIn.textContent = "Pop in"' in APP_JS
    assert 'button.textContent = "Pop in"' in APP_JS
    assert "function popInPanel" in APP_JS


def test_a_new_console_can_open_straight_in_its_own_window():
    assert 'id="settings-console-window"' in (
        Path(__file__).resolve().parent.parent / "app" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    calls = re.findall(r"^\s+surfaceSession\(", APP_JS, flags=re.MULTILINE)
    assert len(calls) == 3  # console, screen, serial
