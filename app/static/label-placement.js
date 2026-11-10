// [label-placement.js] Port label placement (pure geometry, run under node by tests/test_label_placement.py), then the drawing tools (shapes, rotate handle, inline text). Loaded in the order listed in index.html; all scripts share one global scope.
// ---- label placement (pure geometry, no DOM; tests/test_label_placement.py runs this block under node) ----
// Port labels start next to their link endpoint. Labels the user has not moved are then nudged outward / sideways to the
// first free spot, so crowded nodes (a switch with five links) do not stack their labels on top of each other or on the
// icon and the name. A label's text is anchored away from its node (right-aligned on the left side, left-aligned on the
// right), so it never grows back into the icon.
const PORT_LABEL_CHAR_W = 5.1;
const PORT_LABEL_H = 13; // measured text box (12) plus the halo
const PORT_LABEL_STEP = 6;
const PORT_LABEL_SIDE = 9;
const PORT_LABEL_MAX_STEPS = 8;

function labelAnchor(dir) {
  return dir.x > 0.35 ? "start" : dir.x < -0.35 ? "end" : "middle";
}

function labelRect(pos, anchor, text) {
  const w = text.length * PORT_LABEL_CHAR_W + 3;
  const x = anchor === "start" ? pos.x : anchor === "end" ? pos.x - w : pos.x - w / 2;
  return { x: x - 1, y: pos.y - PORT_LABEL_H / 2, w, h: PORT_LABEL_H };
}

function rectsOverlap(a, b) {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

// Does the segment touch the rectangle (an endpoint inside it, or it crosses one of its four edges)?
function segmentHitsRect(seg, r) {
  const inside = (x, y) => x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h;
  if (inside(seg.x1, seg.y1) || inside(seg.x2, seg.y2)) return true;
  const ccw = (ax, ay, bx, by, cx, cy) => (cy - ay) * (bx - ax) > (by - ay) * (cx - ax);
  const crosses = (x3, y3, x4, y4) =>
    ccw(seg.x1, seg.y1, x3, y3, x4, y4) !== ccw(seg.x2, seg.y2, x3, y3, x4, y4) &&
    ccw(seg.x1, seg.y1, seg.x2, seg.y2, x3, y3) !== ccw(seg.x1, seg.y1, seg.x2, seg.y2, x4, y4);
  const x2 = r.x + r.w;
  const y2 = r.y + r.h;
  return crosses(r.x, r.y, x2, r.y) || crosses(x2, r.y, x2, y2) || crosses(x2, y2, r.x, y2) || crosses(r.x, y2, r.x, r.y);
}

// items: [{key, node, text, base: {x, y}, dir: {x, y} (unit, from the node outward), fixed: {x, y} | null}]
// obstacles: rects the labels must stay off (node icons, node names). segments: the link lines {x1, y1, x2, y2, ends}; a
// label stays off the lines of OTHER links (`ends` lists the two label keys a line belongs to) but may lie on its own.
// Returns Map(key -> {x, y, anchor}).
function placePortLabels(items, obstacles, segments = []) {
  const taken = [];
  const out = new Map();
  const put = (item, pos) => {
    const anchor = labelAnchor(item.dir);
    taken.push(labelRect(pos, anchor, item.text));
    out.set(item.key, { x: pos.x, y: pos.y, anchor });
  };
  for (const item of items) if (item.fixed) put(item, item.fixed);
  const free = items
    .filter((item) => !item.fixed)
    .sort((a, b) => (a.node < b.node ? -1 : a.node > b.node ? 1 : Math.atan2(a.dir.y, a.dir.x) - Math.atan2(b.dir.y, b.dir.x)));
  for (const item of free) {
    const anchor = labelAnchor(item.dir);
    const normal = { x: -item.dir.y, y: item.dir.x };
    // every candidate spot, nearest to the default first; the first one without a collision wins, otherwise the one
    // with the least (a rectangle overlap counts more than touching a line)
    const candidates = [];
    for (let k = 0; k <= PORT_LABEL_MAX_STEPS; k++) {
      for (const t of [0, 1, -1, 2, -2, 3, -3]) {
        const pos = {
          x: item.base.x + item.dir.x * k * PORT_LABEL_STEP + normal.x * t * PORT_LABEL_SIDE,
          y: item.base.y + item.dir.y * k * PORT_LABEL_STEP + normal.y * t * PORT_LABEL_SIDE,
        };
        candidates.push({ pos, dist: Math.hypot(pos.x - item.base.x, pos.y - item.base.y) });
      }
    }
    candidates.sort((a, b) => a.dist - b.dist);
    let chosen = item.base;
    let bestPenalty = Infinity;
    for (const { pos } of candidates) {
      const rect = labelRect(pos, anchor, item.text);
      const penalty =
        100 * (obstacles.filter((o) => rectsOverlap(rect, o)).length + taken.filter((o) => rectsOverlap(rect, o)).length) +
        segments.filter((seg) => !(seg.ends || []).includes(item.key) && segmentHitsRect(seg, rect)).length;
      if (penalty < bestPenalty) {
        bestPenalty = penalty;
        chosen = pos;
        if (penalty === 0) break;
      }
    }
    put(item, chosen);
  }
  return out;
}
// ---- end label placement ----

function nodeObstacles() {
  const rects = [];
  for (const n of Object.values(nodesData)) {
    const half = NODE_ICON_SIZE / 2;
    rects.push({ x: n.x - half - 2, y: n.y - half - 2, w: NODE_ICON_SIZE + 4, h: NODE_ICON_SIZE + 4 });
    const moved = labelOffsets[labelKey(n.name)] || { dx: 0, dy: 0 };
    // the name is 11px text with its baseline NODE_ICON_SIZE below the node's centre (see buildNodeShape)
    const w = n.name.length * 7.6 + 4;
    rects.push({ x: n.x + moved.dx - w / 2, y: n.y + NODE_ICON_SIZE + moved.dy - 12, w, h: 16 });
  }
  return rects;
}

// Places every port label: moved ones at their saved spot, the rest on the first free spot (see placePortLabels).
function layoutPortLabels() {
  const labels = [...document.querySelectorAll("#labels-layer .port-label")];
  if (!labels.length) return;
  const items = labels.map((label) => {
    const node = nodesData[label.dataset.for];
    const base = portLabelPoint(node, nodesData[label.dataset.other], Number(label.dataset.offset) || 0, label.dataset.first === "1");
    const len = Math.hypot(base.x - node.x, base.y - node.y) || 1;
    const moved = labelOffsets[label.dataset.key];
    return {
      key: label.dataset.key,
      node: node.name,
      text: label.textContent,
      base,
      dir: { x: (base.x - node.x) / len, y: (base.y - node.y) / len },
      fixed: moved ? { x: base.x + moved.dx, y: base.y + moved.dy } : null,
    };
  });
  const segments = [...document.querySelectorAll("#links-layer .link-line")].map((line) => ({
    x1: Number(line.getAttribute("x1")),
    y1: Number(line.getAttribute("y1")),
    x2: Number(line.getAttribute("x2")),
    y2: Number(line.getAttribute("y2")),
    ends: [line.dataset.endA, line.dataset.endB],
  }));
  const placed = placePortLabels(items, nodeObstacles(), segments);
  for (const label of labels) {
    const at = placed.get(label.dataset.key);
    label.setAttribute("x", at.x);
    label.setAttribute("y", at.y);
    label.setAttribute("text-anchor", at.anchor);
  }
}

let labelLayoutQueued = false;
function scheduleLabelLayout() {
  if (labelLayoutQueued) return;
  labelLayoutQueued = true;
  requestAnimationFrame(() => {
    labelLayoutQueued = false;
    layoutPortLabels();
  });
}

function applyNodeLabelOffset(textEl, nodeName) {
  const moved = labelOffsets[labelKey(nodeName)];
  if (moved) textEl.setAttribute("transform", `translate(${moved.dx}, ${moved.dy})`);
  else textEl.removeAttribute("transform");
}

// Dragging a label moves only the label (stored as an offset, so it follows its node afterwards); a double click
// puts it back. Both kinds of label call this: `reposition` re-applies the position after each change.
function startLabelDrag(evt, key, reposition) {
  if (linkMode || drawMode || evt.button !== 0) return;
  evt.stopPropagation();
  evt.preventDefault();
  const start = svgPoint(evt);
  const base = labelOffsets[key] || { dx: 0, dy: 0 };
  let moved = false;
  const onMove = (e) => {
    const p = svgPoint(e);
    if (!moved && Math.hypot(p.x - start.x, p.y - start.y) < 3) return;
    moved = true;
    labelOffsets[key] = { dx: base.dx + p.x - start.x, dy: base.dy + p.y - start.y };
    reposition();
  };
  const onUp = () => {
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    if (moved) saveLabelOffsets({ [key]: labelOffsets[key] });
  };
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp);
}

function resetLabel(key, reposition) {
  if (!labelOffsets[key]) return;
  delete labelOffsets[key];
  reposition();
  saveLabelOffsets({ [key]: null });
}

function saveLabelOffsets(changes) {
  return fetch(apiPath("/labels"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ labels: changes }),
  }).then((resp) => { if (designMode) refreshHistoryState(); return resp; }).catch(() => {});
}

function appendLinkEndpointDot(layer, nearNode, farNode, ifName, offset = 0, nearIsFirst = true) {
  const p = shiftedEdgePoint(nearNode, farNode, offset, nearIsFirst);
  const dot = document.createElementNS(SVG_NS, "circle");
  dot.setAttribute("class", "link-endpoint");
  dot.setAttribute("r", 3);
  dot.setAttribute("cx", p.x);
  dot.setAttribute("cy", p.y);
  dot.dataset.for = nearNode.name;
  dot.dataset.other = farNode.name;
  dot.dataset.offset = offset;
  dot.dataset.first = nearIsFirst ? "1" : "0";
  layer.appendChild(dot);

  if (!ifName || !showPortLabels) return;
  // Port labels live in their own layer above the nodes, so no icon or name can hide one.
  const label = document.createElementNS(SVG_NS, "text");
  label.setAttribute("class", "port-label");
  label.dataset.for = nearNode.name;
  label.dataset.other = farNode.name;
  label.dataset.offset = offset;
  label.dataset.first = nearIsFirst ? "1" : "0";
  label.dataset.key = labelKey(nearNode.name, ifName);
  label.textContent = ifName;
  const reposition = scheduleLabelLayout;
  label.addEventListener("pointerdown", (evt) => startLabelDrag(evt, label.dataset.key, reposition));
  label.addEventListener("dblclick", () => resetLabel(label.dataset.key, reposition));
  document.getElementById("labels-layer").appendChild(label);
}

function setDrawMode(mode) {
  drawMode = drawMode === mode ? null : mode;
  document.querySelectorAll("#menu-draw .menu-item[data-draw]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.draw === drawMode);
  });
  svg.classList.toggle("drawing", !!drawMode);
}

async function persistDrawings() {
  if (!designMode) {
    const after = cloneDrawings(drawings);
    if (JSON.stringify(after) !== JSON.stringify(lastPersistedDrawings)) {
      pushLiveHistory({ kind: "drawings", before: lastPersistedDrawings, after });
    }
    lastPersistedDrawings = after;
  }
  try {
    await fetch(apiPath("/drawings"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ drawings }),
    });
  } catch (err) {
    // best-effort - drawings already work locally even if this fails
  }
  refreshHistoryState();
}

// True for a color string that already encodes its own alpha
// (rgba()/#rrggbbaa) - a plain "#rrggbb" has none, so a fill in that
// format still gets the old hardcoded 18% fallback below, for visual
// backward compatibility with drawings saved before per-color alpha
// (buildColorField) existed.
function hasExplicitAlpha(color) {
  return /^rgba?\(/i.test(color || "") || /^#[0-9a-f]{8}$/i.test(color || "");
}

// The point every drawing type rotates around - its own visual center,
// not necessarily (x1,y1) (a rect/ellipse/image's anchor is a corner,
// a line's is one endpoint - only text's (x1,y1) already is its own
// rotation-relevant point, its baseline-left anchor).
function drawingCenter(d) {
  if (d.type === "line") return { cx: (d.x1 + d.x2) / 2, cy: (d.y1 + d.y2) / 2 };
  if (d.type === "text") return { cx: d.x1, cy: d.y1 };
  return {
    cx: Math.min(d.x1, d.x2) + Math.abs(d.x2 - d.x1) / 2,
    cy: Math.min(d.y1, d.y2) + Math.abs(d.y2 - d.y1) / 2,
  };
}

function buildDrawingShape(d, isDraft) {
  const x1 = Math.min(d.x1, d.x2);
  const y1 = Math.min(d.y1, d.y2);
  const w = Math.abs(d.x2 - d.x1);
  const h = Math.abs(d.y2 - d.y1);
  let el;
  if (d.type === "rect") {
    el = document.createElementNS(SVG_NS, "rect");
    el.setAttribute("x", x1);
    el.setAttribute("y", y1);
    el.setAttribute("width", w);
    el.setAttribute("height", h);
    el.setAttribute("stroke", d.stroke || "#7cb342");
    el.setAttribute("stroke-width", d.strokeWidth || 2);
    el.setAttribute("fill", d.fill && d.fill !== "none" ? d.fill : "none");
    if (d.fill && d.fill !== "none" && !hasExplicitAlpha(d.fill)) el.setAttribute("fill-opacity", "0.18");
    el.setAttribute("rx", d.rx || 0);
  } else if (d.type === "ellipse") {
    el = document.createElementNS(SVG_NS, "ellipse");
    el.setAttribute("cx", x1 + w / 2);
    el.setAttribute("cy", y1 + h / 2);
    el.setAttribute("rx", w / 2);
    el.setAttribute("ry", h / 2);
    el.setAttribute("stroke", d.stroke || "#7cb342");
    el.setAttribute("stroke-width", d.strokeWidth || 2);
    el.setAttribute("fill", d.fill && d.fill !== "none" ? d.fill : "none");
    if (d.fill && d.fill !== "none" && !hasExplicitAlpha(d.fill)) el.setAttribute("fill-opacity", "0.18");
  } else if (d.type === "line") {
    el = document.createElementNS(SVG_NS, "line");
    el.setAttribute("x1", d.x1);
    el.setAttribute("y1", d.y1);
    el.setAttribute("x2", d.x2);
    el.setAttribute("y2", d.y2);
    el.setAttribute("stroke", d.stroke || "#7cb342");
    el.setAttribute("stroke-width", d.strokeWidth || 2);
  } else if (d.type === "image") {
    // href is a data: URI (see the draw-image-input handler) - images
    // are embedded directly into the drawing object itself rather than
    // uploaded to separate storage, so /drawings and a .topoproj
    // bundle's own drawings.json already carry them with zero backend
    // changes (both already treat a drawing as an opaque JSON object -
    // see app/main.py's own get_drawings/set_drawings docstring).
    el = document.createElementNS(SVG_NS, "image");
    el.setAttribute("x", x1);
    el.setAttribute("y", y1);
    el.setAttribute("width", w);
    el.setAttribute("height", h);
    el.setAttribute("href", d.href || "");
    el.setAttribute("preserveAspectRatio", "none");
  } else {
    el = document.createElementNS(SVG_NS, "text");
    el.setAttribute("x", d.x1);
    el.setAttribute("y", d.y1);
    el.setAttribute("fill", d.stroke || "#1f2328");
    el.style.fontSize = `${d.fontSize || 16}px`;
    el.textContent = d.text || "";
  }
  el.setAttribute("class", `drawing-shape drawing-${d.type}${isDraft ? " drawing-draft" : ""}`);
  if (d.rotation) {
    const { cx, cy } = drawingCenter(d);
    el.setAttribute("transform", `rotate(${d.rotation}, ${cx}, ${cy})`);
  }
  if (!isDraft) {
    el.dataset.drawId = d.id;
    el.addEventListener("pointerdown", (evt) => startDrawingPointerDown(evt, d));
    if (d.type === "text") {
      el.addEventListener("dblclick", (evt) => {
        evt.stopPropagation();
        if (closeOpenDrawingPopup) closeOpenDrawingPopup();
        editTextDrawingInline(d, { isNew: false });
      });
    }
  }
  return el;
}

// A pointerdown on an existing drawing could turn into a move (drag
// its body) or just be a click (open its style/delete popup) - same
// "wait for real movement before treating it as a drag" pattern used
// for panning vs. clicking the canvas itself.
function startDrawingPointerDown(evt, d) {
  evt.stopPropagation();
  if (drawMode) return; // a draw tool is armed - don't hijack the click for moving/selecting
  const startClientX = evt.clientX;
  const startClientY = evt.clientY;
  const startPoint = svgPoint(evt);
  const orig = { x1: d.x1, y1: d.y1, x2: d.x2, y2: d.y2 };
  let moved = false;

  function onMove(e) {
    if (!moved && Math.hypot(e.clientX - startClientX, e.clientY - startClientY) < 4) return;
    moved = true;
    const p = svgPoint(e);
    const dx = p.x - startPoint.x;
    const dy = p.y - startPoint.y;
    d.x1 = orig.x1 + dx;
    d.y1 = orig.y1 + dy;
    if (d.type !== "text") {
      d.x2 = orig.x2 + dx;
      d.y2 = orig.y2 + dy;
    }
    renderDrawings();
  }
  function onUp(e) {
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    if (moved) {
      if (snapToGridEnabled) {
        d.x1 = snapToGrid(d.x1);
        d.y1 = snapToGrid(d.y1);
        if (d.type !== "text") {
          d.x2 = snapToGrid(d.x2);
          d.y2 = snapToGrid(d.y2);
        }
        renderDrawings();
      }
      persistDrawings();
    } else {
      selectedDrawingId = d.id;
      renderDrawings();
      showDrawingActionsPopup(d.id, { x: e.clientX, y: e.clientY });
    }
  }
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp);
}

function buildDrawingHandles(d) {
  const handles = [];
  if (d.type !== "text") {
    // Rotated purely visually (to stay on the shape's own rotated
    // corners) - the drag itself still works in the shape's original,
    // unrotated coordinate space (same simplification every resize
    // handle here already made before rotation existed).
    const { cx, cy } = drawingCenter(d);
    for (const [xKey, yKey] of [["x1", "y1"], ["x2", "y2"]]) {
      const h = document.createElementNS(SVG_NS, "circle");
      h.setAttribute("class", "drawing-handle");
      h.setAttribute("cx", d[xKey]);
      h.setAttribute("cy", d[yKey]);
      h.setAttribute("r", 5);
      if (d.rotation) h.setAttribute("transform", `rotate(${d.rotation}, ${cx}, ${cy})`);
      h.addEventListener("pointerdown", (evt) => startDrawingHandleDrag(evt, d, xKey, yKey));
      handles.push(h);
    }
  }
  handles.push(buildRotateHandle(d));
  return handles;
}

// A stalk + circle above the shape's own top (its center for a line/
// text, which have no real "top"), wrapped in the shape's own current
// rotate() transform so it visually orbits with it - dragging computes
// a fresh angle from the live pointer position on every move, entirely
// independent of the handle's own (rotated) rendered position.
const ROTATE_HANDLE_OFFSET = 28;
function rotateHandleAnchor(d) {
  const { cx, cy } = drawingCenter(d);
  if (d.type === "line" || d.type === "text") {
    return { cx, top: cy - ROTATE_HANDLE_OFFSET };
  }
  return { cx, top: Math.min(d.y1, d.y2) - ROTATE_HANDLE_OFFSET };
}
function buildRotateHandle(d) {
  const { cx, cy } = drawingCenter(d);
  const { top } = rotateHandleAnchor(d);
  const group = document.createElementNS(SVG_NS, "g");
  if (d.rotation) group.setAttribute("transform", `rotate(${d.rotation}, ${cx}, ${cy})`);
  const stalk = document.createElementNS(SVG_NS, "line");
  stalk.setAttribute("class", "drawing-rotate-line");
  stalk.setAttribute("x1", cx);
  stalk.setAttribute("y1", cy);
  stalk.setAttribute("x2", cx);
  stalk.setAttribute("y2", top);
  group.appendChild(stalk);
  const handle = document.createElementNS(SVG_NS, "circle");
  handle.setAttribute("class", "drawing-rotate-handle");
  handle.setAttribute("cx", cx);
  handle.setAttribute("cy", top);
  handle.setAttribute("r", 5);
  handle.addEventListener("pointerdown", (evt) => startDrawingRotateDrag(evt, d));
  group.appendChild(handle);
  return group;
}
function startDrawingRotateDrag(evt, d) {
  evt.stopPropagation();
  const { cx, cy } = drawingCenter(d);
  function angleAt(e) {
    const p = svgPoint(e);
    let deg = Math.atan2(p.y - cy, p.x - cx) * (180 / Math.PI) + 90;
    deg = ((deg % 360) + 360) % 360;
    if (e.shiftKey) deg = Math.round(deg / 15) * 15 % 360;
    return deg;
  }
  function onMove(e) {
    d.rotation = angleAt(e);
    renderDrawings();
  }
  function onUp() {
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    persistDrawings();
  }
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp);
}

function startDrawingHandleDrag(evt, d, xKey, yKey) {
  evt.stopPropagation();
  function onMove(e) {
    const p = svgPoint(e);
    d[xKey] = snapToGridEnabled ? snapToGrid(p.x) : p.x;
    d[yKey] = snapToGridEnabled ? snapToGrid(p.y) : p.y;
    renderDrawings();
  }
  function onUp() {
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    persistDrawings();
  }
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp);
}

function renderDrawings() {
  const layer = document.getElementById("drawings-layer");
  layer.innerHTML = "";
  for (const d of drawings) {
    if (d.id === editingDrawingId) continue; // hidden behind its own inline canvas editor right now
    layer.appendChild(buildDrawingShape(d));
    if (d.id === selectedDrawingId) {
      for (const handle of buildDrawingHandles(d)) layer.appendChild(handle);
    }
  }
  if (drawDraft) layer.appendChild(buildDrawingShape(drawDraft, true));
}

// Text is edited directly on the canvas, not through a popup - a plain
// HTML <input> positioned right on top of where the SVG <text> would
// be (svgToScreenPoint handles the coordinate conversion, including
// the current zoom level), the real <text> hidden underneath for as
// long as this is open (see renderDrawings's own editingDrawingId
// check above). Enter or clicking away commits; Escape cancels - for
// a brand-new text (opts.isNew), canceling or committing empty removes
// the drawing entirely rather than leaving a blank annotation behind.
function editTextDrawingInline(d, opts) {
  const isNew = !!(opts && opts.isNew);
  const originalText = d.text || "";
  editingDrawingId = d.id;
  renderDrawings();

  const screenPt = svgToScreenPoint(d.x1, d.y1);
  const scale = svg.clientWidth / viewBoxState.w;
  const fontSize = Math.max((d.fontSize || 16) * scale, 10);

  const input = document.createElement("input");
  input.type = "text";
  input.className = "canvas-text-editor";
  input.value = originalText;
  input.style.left = `${screenPt.x}px`;
  input.style.top = `${screenPt.y - fontSize}px`;
  input.style.fontSize = `${fontSize}px`;
  input.style.color = d.stroke || "#1f2328";
  document.body.appendChild(input);
  input.focus();
  input.select();

  let done = false;
  function cleanup() {
    input.removeEventListener("keydown", onKeydown);
    input.removeEventListener("blur", onBlur);
    input.remove();
    editingDrawingId = null;
  }
  function commit() {
    if (done) return;
    done = true;
    cleanup();
    const value = input.value.trim();
    if (!value) {
      drawings = drawings.filter((x) => x.id !== d.id);
    } else {
      d.text = value;
    }
    renderDrawings();
    persistDrawings();
  }
  function cancel() {
    if (done) return;
    done = true;
    cleanup();
    if (isNew) drawings = drawings.filter((x) => x.id !== d.id);
    renderDrawings();
  }
  function onKeydown(evt) {
    if (evt.key === "Enter") { evt.preventDefault(); commit(); }
    else if (evt.key === "Escape") { evt.preventDefault(); cancel(); }
  }
  function onBlur() { commit(); }
  input.addEventListener("keydown", onKeydown);
  input.addEventListener("blur", onBlur);
}
