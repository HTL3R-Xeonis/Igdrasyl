"""
The automatic placement of port labels (the frontend scripts, the block between the "label placement" markers) is pure geometry,
so it is run here under node. Skipped where node is not installed.
"""

import json
import math
import shutil
import subprocess

import pytest
from frontend_js import read_app_js

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)


def _block() -> str:
    js = read_app_js()
    start = js.index("// ---- label placement")
    end = js.index("// ---- end label placement ----")
    return js[start:end]


def _place(items, obstacles, segments=None):
    texts = {i["key"]: i["text"] for i in items}
    driver = f"""{_block()}
const out = placePortLabels({json.dumps(items)}, {json.dumps(obstacles)}, {json.dumps(segments or [])});
const texts = {json.dumps(texts)};
console.log(JSON.stringify([...out.entries()]));
console.log(JSON.stringify([...out.entries()].map(([k, v]) => [k, labelRect(v, v.anchor, texts[k])])));
"""
    result = subprocess.run(
        ["node", "-e", driver], capture_output=True, text=True, check=True, timeout=30
    )
    placed, rects = result.stdout.strip().splitlines()
    return dict(json.loads(placed)), dict(json.loads(rects))


def _touches(segment, rect):
    """Does the line segment pass through the rectangle (sampled densely - exact enough for a test)?"""
    steps = 600
    for i in range(steps + 1):
        x = segment["x1"] + (segment["x2"] - segment["x1"]) * i / steps
        y = segment["y1"] + (segment["y2"] - segment["y1"]) * i / steps
        if (
            rect["x"] <= x <= rect["x"] + rect["w"]
            and rect["y"] <= y <= rect["y"] + rect["h"]
        ):
            return True
    return False


def _overlap(a, b):
    return (
        a["x"] < b["x"] + b["w"]
        and b["x"] < a["x"] + a["w"]
        and a["y"] < b["y"] + b["h"]
        and b["y"] < a["y"] + a["h"]
    )


def _item(key, node, text, centre, angle_deg, fixed=None):
    angle = math.radians(angle_deg)
    direction = {"x": math.cos(angle), "y": math.sin(angle)}
    # the default spot: just outside a 32px icon, 12px further out (what portLabelPoint gives)
    reach = 16 + 12
    base = {
        "x": centre[0] + direction["x"] * reach,
        "y": centre[1] + direction["y"] * reach,
    }
    return {
        "key": key,
        "node": node,
        "text": text,
        "base": base,
        "dir": direction,
        "fixed": fixed,
    }


def _icon(centre):
    return {"x": centre[0] - 18, "y": centre[1] - 18, "w": 36, "h": 36}


def _crowded_switch():
    centre = (400, 300)
    # five links fanning out below the switch, as in the mini office: Gi0/1 .. Gi1/0 within a narrow angle
    items = [
        _item(f"SW1|Gi0/{i}", "SW1", f"Gi0/{i}", centre, 100 + i * 12)
        for i in range(1, 5)
    ]
    items.append(_item("SW1|Gi1/0", "SW1", "Gi1/0", centre, 70))
    name = {"x": 400 - 13, "y": 300 + 20, "w": 26.8, "h": 16}
    return items, [_icon(centre), name]


def _spread_switch():
    """Five links over about 100 degrees (25 degrees apart), like the mini office's switch."""
    centre = (400, 300)
    items = [
        _item(f"SW1|Gi0/{i}", "SW1", f"Gi0/{i}", centre, 60 + i * 25) for i in range(5)
    ]
    return items, [_icon(centre), {"x": 387, "y": 320, "w": 26.8, "h": 16}]


def _link_lines(items, centre=(400, 300), length=300):
    """The links the items belong to: a line from the node's centre outward along each label's direction."""
    return [
        {
            "x1": centre[0],
            "y1": centre[1],
            "x2": centre[0] + i["dir"]["x"] * length,
            "y2": centre[1] + i["dir"]["y"] * length,
            "ends": [i["key"]],
        }
        for i in items
    ]


def test_crowded_labels_no_longer_overlap_each_other_the_icon_or_the_name():
    items, obstacles = _crowded_switch()
    _, rects = _place(items, obstacles)
    keys = list(rects)
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            assert not _overlap(rects[a], rects[b]), (a, b)
        for obstacle in obstacles:
            assert not _overlap(rects[a], obstacle), a


def test_a_label_with_room_stays_on_its_default_spot():
    item = _item("R1|Gi0/0", "R1", "Gi0/0", (100, 100), 0)
    placed, _ = _place([item], [_icon((100, 100))])
    assert placed["R1|Gi0/0"]["x"] == pytest.approx(item["base"]["x"])
    assert placed["R1|Gi0/0"]["y"] == pytest.approx(item["base"]["y"])


def test_the_text_grows_away_from_the_node():
    left = _item("A|l", "A", "Gi0/0", (100, 100), 180)
    right = _item("A|r", "A", "Gi0/1", (100, 100), 0)
    below = _item("A|b", "A", "Gi0/2", (100, 100), 90)
    placed, _ = _place([left, right, below], [_icon((100, 100))])
    assert placed["A|l"]["anchor"] == "end"
    assert placed["A|r"]["anchor"] == "start"
    assert placed["A|b"]["anchor"] == "middle"


def test_a_label_the_user_moved_stays_exactly_where_it_was_put():
    items, obstacles = _crowded_switch()
    items[0]["fixed"] = {"x": 700.0, "y": 50.0}
    placed, rects = _place(items, obstacles)
    assert placed[items[0]["key"]] == {
        "x": 700.0,
        "y": 50.0,
        "anchor": placed[items[0]["key"]]["anchor"],
    }
    # the others still avoid it
    for item in items[1:]:
        assert not _overlap(rects[item["key"]], rects[items[0]["key"]])


def test_placement_is_deterministic():
    items, obstacles = _crowded_switch()
    first, _ = _place(items, obstacles)
    second, _ = _place(list(reversed(items)), obstacles)
    assert first == second


def test_when_nothing_is_free_the_label_still_gets_a_position():
    item = _item("R1|Gi0/0", "R1", "Gi0/0", (100, 100), 0)
    wall = {"x": -1000, "y": -1000, "w": 5000, "h": 5000}
    placed, _ = _place([item], [wall])
    assert placed["R1|Gi0/0"]["x"] == pytest.approx(item["base"]["x"])


def test_labels_stay_off_the_lines_of_other_links():
    items, obstacles = _spread_switch()
    segments = _link_lines(items)
    _, rects = _place(items, obstacles, segments)
    for key, rect in rects.items():
        for segment in segments:
            if key in segment["ends"]:
                continue  # its own line: allowed
            assert not _touches(segment, rect), key
    keys = list(rects)
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            assert not _overlap(rects[a], rects[b]), (a, b)


def test_a_label_may_stay_on_its_own_line():
    item = _item("R1|Gi0/0", "R1", "Gi0/0", (100, 100), 0)
    segments = _link_lines([item], centre=(100, 100))
    placed, rects = _place([item], [_icon((100, 100))], segments)
    # the default spot lies on its own link and is kept (nothing else is in the way)
    assert placed["R1|Gi0/0"]["x"] == pytest.approx(item["base"]["x"])
    assert _touches(segments[0], rects["R1|Gi0/0"])


def test_a_foreign_line_through_the_default_spot_pushes_the_label_away():
    item = _item("R1|Gi0/0", "R1", "Gi0/0", (100, 100), 0)
    # another link's line running straight through where the label would sit
    foreign = {
        "x1": item["base"]["x"] + 8,
        "y1": -500,
        "x2": item["base"]["x"] + 8,
        "y2": 700,
        "ends": ["X|Gi0/9"],
    }
    _, rects = _place([item], [_icon((100, 100))], [foreign])
    assert not _touches(foreign, rects["R1|Gi0/0"])


def test_a_crowded_spot_still_gets_the_least_bad_position_not_a_crash():
    items, obstacles = _crowded_switch()
    # lines everywhere: no spot is free, every label must still get a position
    fence = [{"x1": -2000, "y1": y, "x2": 3000, "y2": y} for y in range(-2000, 2000, 4)]
    placed, _ = _place(items, obstacles, fence)
    assert set(placed) == {i["key"] for i in items}
