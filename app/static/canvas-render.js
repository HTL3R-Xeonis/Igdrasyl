// [canvas-render.js] Rendering and using the canvas: svg root, zoom, node dragging, selection and the multi-selection popup. Loaded in the order listed in index.html; all scripts share one global scope.
const svg = document.getElementById("canvas");
let viewBoxState = { x: 0, y: 0, w: 1600, h: 900 };

function applyViewBox() {
  svg.setAttribute("viewBox", `${viewBoxState.x} ${viewBoxState.y} ${viewBoxState.w} ${viewBoxState.h}`);
}

function zoomBy(scale, center) {
  viewBoxState.x = center.x - (center.x - viewBoxState.x) * scale;
  viewBoxState.y = center.y - (center.y - viewBoxState.y) * scale;
  viewBoxState.w *= scale;
  viewBoxState.h *= scale;
  applyViewBox();
  updateStatusBar(true);
}

// Settings menu's own zoom field - sets the zoom to an exact percent
// (matching the same 1600-wide baseline updateStatusBar's own readout
// uses) instead of a relative +/-/scroll step, centered on whatever's
// currently in view.
function setZoomPercent(percent) {
  if (!isFinite(percent) || percent <= 0) return;
  const targetWidth = 1600 / (percent / 100);
  const scale = targetWidth / viewBoxState.w;
  zoomBy(scale, { x: viewBoxState.x + viewBoxState.w / 2, y: viewBoxState.y + viewBoxState.h / 2 });
}

function svgPoint(evt) {
  const pt = svg.createSVGPoint();
  pt.x = evt.clientX;
  pt.y = evt.clientY;
  return pt.matrixTransform(svg.getScreenCTM().inverse());
}

// The inverse of svgPoint - an SVG user-space point (e.g. a text
// drawing's own x1/y1) to real viewport/screen pixels, for positioning
// a plain HTML overlay (the inline text editor) exactly on top of it.
function svgToScreenPoint(svgX, svgY) {
  const pt = svg.createSVGPoint();
  pt.x = svgX;
  pt.y = svgY;
  const screenPt = pt.matrixTransform(svg.getScreenCTM());
  return { x: screenPt.x, y: screenPt.y };
}

let dragState = null;

function moveNodeShape(name) {
  const node = nodesData[name];
  const g = document.querySelector(`.node-shape[data-name="${name}"]`);
  if (g) g.setAttribute("transform", `translate(${node.x - NODE_W / 2}, ${node.y - NODE_ICON_SIZE / 2})`);
  document.querySelectorAll(".link-line, .link-hit").forEach((line) => {
    const { a, b } = line.dataset;
    if (a !== name && b !== name) return;
    setLineEndpoints(line, nodesData[a], nodesData[b], Number(line.dataset.offset) || 0);
  });
  document.querySelectorAll(".link-endpoint").forEach((dot) => {
    if (dot.dataset.for !== name && dot.dataset.other !== name) return;
    const p2 = shiftedEdgePoint(nodesData[dot.dataset.for], nodesData[dot.dataset.other], Number(dot.dataset.offset) || 0, dot.dataset.first === "1");
    dot.setAttribute("cx", p2.x);
    dot.setAttribute("cy", p2.y);
  });
  document.querySelectorAll(".port-label").forEach((label) => {
    if (label.dataset.for !== name && label.dataset.other !== name) return;
    scheduleLabelLayout();
  });
}

function startNodeDrag(evt, name) {
  evt.stopPropagation();
  if (linkMode) {
    handleLinkModeClick(name, { x: evt.clientX, y: evt.clientY });
    return;
  }
  const anchor = { x: evt.clientX, y: evt.clientY };
  // Ctrl/Cmd/Shift-click adds or removes a node from the selection, no drag.
  if (evt.ctrlKey || evt.metaKey || evt.shiftKey) {
    toggleNodeSelection(name, anchor);
    return;
  }
  // Grabbing a node that is already part of a multi-selection moves the
  // whole group; anything else selects just this node first.
  if (!(selectedNames.size > 1 && selectedNames.has(name))) selectNode(name, anchor);
  const node = nodesData[name];
  const p = svgPoint(evt);
  const members = [...selectedNames].filter((n) => nodesData[n]);
  dragState = {
    name,
    offsetX: p.x - node.x,
    offsetY: p.y - node.y,
    moved: false,
    start: Object.fromEntries(members.map((n) => [n, { x: nodesData[n].x, y: nodesData[n].y }])),
  };
  window.addEventListener("pointermove", onNodeDrag);
  window.addEventListener("pointerup", endNodeDrag);
}

function onNodeDrag(evt) {
  if (!dragState) return;
  dragState.moved = true;
  const p = svgPoint(evt);
  const lead = nodesData[dragState.name];
  let x = p.x - dragState.offsetX;
  let y = p.y - dragState.offsetY;
  if (snapToGridEnabled) {
    x = snapToGrid(x);
    y = snapToGrid(y);
  }
  const dx = x - dragState.start[dragState.name].x;
  const dy = y - dragState.start[dragState.name].y;
  for (const [name, origin] of Object.entries(dragState.start)) {
    nodesData[name].x = origin.x + dx;
    nodesData[name].y = origin.y + dy;
  }
  lead.x = x;
  lead.y = y;
  for (const name of Object.keys(dragState.start)) moveNodeShape(name);
}

function endNodeDrag() {
  window.removeEventListener("pointermove", onNodeDrag);
  window.removeEventListener("pointerup", endNodeDrag);
  if (dragState && dragState.moved) {
    const before = dragState.start;
    const after = Object.fromEntries(Object.keys(before).map((n) => [n, { x: nodesData[n].x, y: nodesData[n].y }]));
    commitMove(before, after);
  }
  dragState = null;
}

// Saves a move of one or more nodes in a single request (so it is one undo
// step) - see also undoLastMove for live mode's own history.
function commitMove(before, after) {
  if (!designMode) pushLiveHistory({ kind: "move", before, after });
  postPositions(after).then(refreshHistoryState);
}

function postPositions(positions) {
  return fetch(apiPath("/layout/batch"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ positions }),
  });
}

svg.addEventListener("wheel", (evt) => {
  evt.preventDefault();
  const p = svgPoint(evt);
  zoomBy(evt.deltaY > 0 ? 1.1 : 0.9, p);
});

// Drag-and-drop a device row straight from the palette onto the
// canvas - an alternative to click-to-arm-then-click-canvas, not a
// replacement for it.
svg.addEventListener("dragover", (evt) => {
  evt.preventDefault();
  evt.dataTransfer.dropEffect = "copy";
});
svg.addEventListener("drop", (evt) => {
  evt.preventDefault();
  let data;
  try {
    data = JSON.parse(evt.dataTransfer.getData("application/json"));
  } catch (err) {
    return;
  }
  if (!data || !data.image) return;
  placementImage = data.image;
  placementRole = data.role;
  placeNodeAt(svgPoint(evt));
});

let panState = null;
let bandState = null; // {x1,y1,x2,y2,additive,moved} while a rubber band is being dragged

function renderSelectionBand() {
  const rect = document.getElementById("selection-rect");
  if (!bandState || !bandState.moved) { rect.hidden = true; return; }
  rect.hidden = false;
  rect.setAttribute("x", Math.min(bandState.x1, bandState.x2));
  rect.setAttribute("y", Math.min(bandState.y1, bandState.y2));
  rect.setAttribute("width", Math.abs(bandState.x2 - bandState.x1));
  rect.setAttribute("height", Math.abs(bandState.y2 - bandState.y1));
}
svg.addEventListener("pointerdown", (evt) => {
  if (evt.target !== svg) return;
  if (placementImage) {
    placeNodeAt(svgPoint(evt));
    return;
  }
  if (drawMode) {
    const p = svgPoint(evt);
    if (drawMode === "text") {
      setDrawMode(null);
      const d = { id: ++drawIdCounter, type: "text", x1: p.x, y1: p.y, text: "", stroke: "#1f2328", fontSize: 16 };
      drawings.push(d);
      editTextDrawingInline(d, { isNew: true });
      return;
    }
    drawDraft = { type: drawMode, x1: p.x, y1: p.y, x2: p.x, y2: p.y, stroke: "#7cb342", fill: "none", strokeWidth: 2 };
    renderDrawings();
    return;
  }
  if (evt.shiftKey) {
    // Shift-drag on empty canvas: rubber-band selection (a plain drag pans).
    const p = svgPoint(evt);
    bandState = { x1: p.x, y1: p.y, x2: p.x, y2: p.y, additive: evt.ctrlKey || evt.metaKey, moved: false };
    return;
  }
  panState = { startX: evt.clientX, startY: evt.clientY, startVB: { ...viewBoxState } };
  svg.classList.add("panning");
});
window.addEventListener("pointermove", (evt) => {
  if (bandState) {
    const p = svgPoint(evt);
    bandState.x2 = p.x;
    bandState.y2 = p.y;
    bandState.moved = bandState.moved || Math.abs(p.x - bandState.x1) > 3 || Math.abs(p.y - bandState.y1) > 3;
    renderSelectionBand();
    return;
  }
  if (drawDraft) {
    const p = svgPoint(evt);
    drawDraft.x2 = p.x;
    drawDraft.y2 = p.y;
    renderDrawings();
    return;
  }
  if (!panState) return;
  const dx = (evt.clientX - panState.startX) * (viewBoxState.w / svg.clientWidth);
  const dy = (evt.clientY - panState.startY) * (viewBoxState.h / svg.clientHeight);
  viewBoxState.x = panState.startVB.x - dx;
  viewBoxState.y = panState.startVB.y - dy;
  applyViewBox();
});
window.addEventListener("pointerup", (evt) => {
  if (bandState) {
    const band = bandState;
    bandState = null;
    renderSelectionBand();
    if (band.moved) {
      ignoreNextDocumentClick = true;
      setTimeout(() => { ignoreNextDocumentClick = false; }, 50);
      const left = Math.min(band.x1, band.x2), right = Math.max(band.x1, band.x2);
      const top = Math.min(band.y1, band.y2), bottom = Math.max(band.y1, band.y2);
      const inside = Object.values(nodesData)
        .filter((n) => n.x >= left && n.x <= right && n.y >= top && n.y <= bottom)
        .map((n) => n.name);
      const names = band.additive ? new Set([...selectedNames, ...inside]) : new Set(inside);
      setSelection(names, { x: evt.clientX, y: evt.clientY });
    }
    return;
  }
  if (drawDraft) {
    // A plain click with barely any drag gets a sensible default size
    // instead of a degenerate sliver - GNS3's own drawing tools do the
    // same for a click-without-drag.
    if (Math.abs(drawDraft.x2 - drawDraft.x1) < 4 && Math.abs(drawDraft.y2 - drawDraft.y1) < 4) {
      drawDraft.x2 = drawDraft.x1 + 160;
      drawDraft.y2 = drawDraft.y1 + 100;
    }
    drawings.push({ ...drawDraft, id: ++drawIdCounter });
    drawDraft = null;
    renderDrawings();
    persistDrawings();
    setDrawMode(null);
  }
  if (panState) {
    panState = null;
    svg.classList.remove("panning");
  }
});

document.getElementById("zoom-in-btn").addEventListener("click", () => {
  zoomBy(0.9, { x: viewBoxState.x + viewBoxState.w / 2, y: viewBoxState.y + viewBoxState.h / 2 });
});
document.getElementById("zoom-out-btn").addEventListener("click", () => {
  zoomBy(1.1, { x: viewBoxState.x + viewBoxState.w / 2, y: viewBoxState.y + viewBoxState.h / 2 });
});
document.getElementById("zoom-fit-btn").addEventListener("click", () => {
  fitViewToNodes();
});

// Computes a viewBox that contains every current node (plus padding),
// preserving the canvas's own default 16:9 aspect ratio so nothing
// looks stretched and the zoom-percent readout stays meaningful. Never
// zooms in tighter than the default 1600x900 "page" for a sparse
// topology - only zooms out further when nodes actually spread wider
// than that.
function boundingBoxOfNodes(padding) {
  const nodes = Object.values(nodesData);
  if (!nodes.length) return null;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const n of nodes) {
    minX = Math.min(minX, n.x - NODE_W / 2);
    maxX = Math.max(maxX, n.x + NODE_W / 2);
    minY = Math.min(minY, n.y - NODE_H / 2);
    maxY = Math.max(maxY, n.y + NODE_H / 2);
  }
  const p = padding ?? 80;
  return { x: minX - p, y: minY - p, w: maxX - minX + p * 2, h: maxY - minY + p * 2 };
}

function fitViewToNodes() {
  const box = boundingBoxOfNodes();
  if (!box) return;
  const baseAspect = 1600 / 900;
  let w = box.w;
  let h = box.h;
  if (w / h > baseAspect) {
    h = w / baseAspect;
  } else {
    w = h * baseAspect;
  }
  w = Math.max(w, 1600);
  h = w / baseAspect;
  const cx = box.x + box.w / 2;
  const cy = box.y + box.h / 2;
  viewBoxState = { x: cx - w / 2, y: cy - h / 2, w, h };
  applyViewBox();
  updateStatusBar(true);
}

function refreshSelectionHighlight() {
  updateStatusBar();
  document.querySelectorAll(".node-shape").forEach((g) => {
    g.classList.toggle("selected", selectedNames.has(g.dataset.name));
  });
  document.querySelectorAll(".summary-row").forEach((row) => {
    row.classList.toggle("selected", selectedNames.has(row.querySelector(".summary-name").textContent));
  });
}

function selectNode(name, anchor) {
  deselectDrawing();
  selectedName = name;
  selectedNames = new Set([name]);
  hideMultiActions();
  refreshSelectionHighlight();
  showNodeActions(name, anchor);
}

// Replaces the selection. One node behaves exactly like a plain click
// (single-node popup); several get the selection popup instead.
function setSelection(names, anchor) {
  const valid = [...names].filter((n) => nodesData[n]);
  if (valid.length === 0) { deselectNode(); return; }
  if (valid.length === 1) { selectNode(valid[0], anchor); return; }
  deselectDrawing();
  selectedName = null;
  selectedNames = new Set(valid);
  document.getElementById("node-actions").hidden = true;
  refreshSelectionHighlight();
  showMultiActions(anchor);
}

function toggleNodeSelection(name, anchor) {
  const next = new Set(selectedNames);
  if (next.has(name)) next.delete(name);
  else next.add(name);
  setSelection(next, anchor);
}

function hideMultiActions() {
  document.getElementById("multi-actions").hidden = true;
}

function showMultiActions(anchor) {
  const names = [...selectedNames];
  const popup = document.getElementById("multi-actions");
  popup.hidden = false;
  document.getElementById("multi-count").textContent = `${names.length} nodes selected`;
  const withLive = names.filter((n) => nodesData[n].live).length;
  document.getElementById("multi-note").textContent = designMode ? "" : `${withLive} live`;
  for (const id of ["multi-start-btn", "multi-stop-btn", "multi-reload-btn"]) {
    const btn = document.getElementById(id);
    btn.hidden = designMode;
    btn.disabled = withLive === 0;
  }
  document.getElementById("multi-copy-btn").hidden = !designMode;
  positionNear(popup, anchor);
}

function showNodeActions(name, anchor) {
  const node = nodesData[name];
  const isVM = node.environment === "ON_ESXI";
  const live = node.live;
  const status = live && live.status ? live.status : "none";

  const popup = document.getElementById("node-actions");
  popup.hidden = false;
  document.getElementById("selected-role").textContent = node.environment.replace("ON_", "");
  document.getElementById("selected-name").textContent = node.name;
  document.getElementById("selected-status").textContent = `(${node.role}, ${status})`;

  // A VM bridged into GNS3 via a Cloud-node link reports its live
  // status as GNS3's own "started"/"stopped" (see main.py's /nodes -
  // once a live GNS3 entry exists for a node, that's what's used,
  // never falling back to the ESXi-side "poweredOn"/"poweredOff"
  // string) - so "is this VM usable" has to accept either spelling,
  // not just the ESXi one.
  const running = status === "started" || status === "poweredOn";

  const btn = document.getElementById("selected-action-btn");
  const canConsole = !isVM && live && live.console_type === "telnet" && live.status === "started";
  btn.hidden = isVM;
  btn.textContent = "Console";
  btn.disabled = !canConsole;
  btn.title = canConsole ? "Open this node's console" : (live ? `console type: ${live.console_type || "n/a"}, status: ${status}` : "no live data");
  btn.onclick = () => openConsole(name);

  const screenBtn = document.getElementById("selected-screen-btn");
  screenBtn.hidden = !isVM;
  if (isVM) {
    screenBtn.disabled = !running;
    screenBtn.title = running ? "Open this VM's graphical screen" : `VM status: ${status}`;
    screenBtn.onclick = () => openVmScreen(name);
  }
  refreshSerialButton(name, isVM && !designMode);
  const stopped = status === "stopped" || status === "poweredOff";
  const hasLive = !!live;

  const startBtn = document.getElementById("selected-start-btn");
  const stopBtn = document.getElementById("selected-stop-btn");
  const reloadBtn = document.getElementById("selected-reload-btn");
  const noteBtn = document.getElementById("selected-note-btn");
  noteBtn.textContent = nodeNotes[name] ? "Edit note" : "Note";
  noteBtn.onclick = () => openNodeNote(name);
  startBtn.disabled = !hasLive || running;
  stopBtn.disabled = !hasLive || stopped;
  reloadBtn.disabled = !hasLive || !running;
  startBtn.title = hasLive ? "Start this node" : "No live instance to start";
  stopBtn.title = hasLive ? "Stop this node" : "No live instance to stop";
  reloadBtn.title = hasLive ? "Reload this node" : "No live instance to reload";
  startBtn.onclick = () => callLifecycle("start", name);
  stopBtn.onclick = async () => {
    if (await showConfirm(`Stop node "${name}"?`, { danger: true, confirmLabel: "Stop" })) callLifecycle("stop", name);
  };
  reloadBtn.onclick = async () => {
    if (await showConfirm(`Reload node "${name}"? This restarts it from scratch.`, { danger: true, confirmLabel: "Reload" })) callLifecycle("reload", name);
  };

  showTab("console");
  positionNear(popup, anchor);
}

// "Serial console" entry of a VM's popup: opens the VM's serial line in a terminal tab when it has a network serial
// port (which the config push can use as well), otherwise offers to add one.
async function refreshSerialButton(name, applicable) {
  const button = document.getElementById("selected-serial-btn");
  button.hidden = !applicable;
  if (!applicable) return;
  button.disabled = true;
  button.textContent = "Serial console…";
  button.title = "";
  button.onclick = null;
  try {
    const resp = await fetch(`/nodes/${encodeURIComponent(name)}/serial`);
    const state = await resp.json();
    if (selectedName !== name) return;
    if (!resp.ok) throw new Error(state.detail || resp.status);
    if (state.enabled) {
      if (!state.powered_on) {
        button.textContent = "Serial console (VM is off)";
        return;
      }
      button.disabled = false;
      button.textContent = "Serial console";
      button.title = `Opens the serial line (port ${state.port}) in a terminal. It takes one session at a time, so a running config push or another open console blocks it.`;
      button.onclick = () => openSerialConsole(name);
      return;
    }
    button.disabled = false;
    button.textContent = "Enable serial console";
    button.title = "Adds a network serial port - the VM is powered off and on again for that.";
    button.onclick = async () => {
      if (!await showConfirm(
        `Add a serial console to "${name}"?\n\nThe VM is powered off and on again for that (a short interruption). ` +
        `For Linux with VMware Tools a login prompt on the serial line is set up as well; appliances ` +
        `(MikroTik, pfSense, OPNsense) must bring their own serial console.`,
        { title: "Enable serial console", confirmLabel: "Enable", danger: true }
      )) return;
      button.disabled = true;
      setMenubarBusy(`Adding a serial console to "${name}"…`);
      try {
        const done = await fetch(`/nodes/${encodeURIComponent(name)}/serial/enable`, { method: "POST" });
        const body = await done.json();
        if (!done.ok) throw new Error(typeof body.detail === "string" ? body.detail : "failed");
        setMenubarMessage(`Serial console of "${name}" is on port ${body.port} (guest: ${body.guest}).`);
        await loadCanvas();
        if (selectedName === name) showNodeActions(name);
      } catch (err) {
        clearMenubarStatus();
        await showAlert(`Couldn't enable the serial console: ${err.message || err}`, { title: "Serial console" });
        button.disabled = false;
      }
    };
  } catch (err) {
    if (selectedName === name) button.textContent = "Serial console (unavailable)";
  }
}

async function callLifecycle(action, name) {
  try {
    const resp = await fetch(`/nodes/${encodeURIComponent(name)}/${action}`, { method: "POST" });
    if (!resp.ok) {
      await showAlert(`Failed to ${action} node: ${(await resp.json()).detail || resp.status}`);
      return;
    }
    await loadCanvas();
    if (selectedName === name) showNodeActions(name);
  } catch (err) {
    await showAlert(`Failed to ${action} node: ${err}`);
  }
}

document.getElementById("selected-delete-btn").addEventListener("click", async () => {
  const name = selectedName;
  if (!name) return;
  const deleteMsg = designMode
    ? `Delete node "${name}"? This also removes any links to it.`
    : `Delete node "${name}"? This removes its live GNS3/ESXi instance and any links to it.`;
  if (!await showConfirm(deleteMsg, { danger: true, confirmLabel: "Delete" })) return;
  try {
    const resp = await fetch(apiPath(`/nodes/${encodeURIComponent(name)}`), { method: "DELETE" });
    if (!resp.ok) {
      await showAlert(`Failed to delete node: ${(await resp.json()).detail || resp.status}`);
      return;
    }
    closeSessionsForNode(name);
    deselectNode();
    await loadCanvas();
  } catch (err) {
    await showAlert(`Failed to delete node: ${err}`);
  }
});
