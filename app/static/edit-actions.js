// [edit-actions.js] Editing: undo/redo, copy/paste, find node, keyboard shortcuts, GNS3 port names and link deletion. Loaded in the order listed in index.html; all scripts share one global scope.
// --- Undo / redo ---
// Design mode: the server keeps the history (app/design_history.py) - every
// design change is already a step there, we only ask it to undo/redo.
// Live mode: node/link changes are real VMs and not undoable, so only
// moves and drawings have a (client-side) history.
const LIVE_HISTORY_LIMIT = 100;
let liveHistory = { undo: [], redo: [] };
let lastPersistedDrawings = [];
let historyState = { can_undo: false, can_redo: false };

function cloneDrawings(list) {
  return JSON.parse(JSON.stringify(list));
}

function pushLiveHistory(entry) {
  liveHistory.undo.push(entry);
  if (liveHistory.undo.length > LIVE_HISTORY_LIMIT) liveHistory.undo.shift();
  liveHistory.redo = [];
}

async function refreshHistoryState() {
  if (designMode) {
    try {
      historyState = await fetch("/design/history").then((r) => r.json());
    } catch (err) {
      historyState = { can_undo: false, can_redo: false };
    }
  } else {
    historyState = { can_undo: liveHistory.undo.length > 0, can_redo: liveHistory.redo.length > 0 };
  }
  document.getElementById("edit-undo").disabled = !historyState.can_undo;
  document.getElementById("edit-redo").disabled = !historyState.can_redo;
}

async function reloadDrawingsFromServer() {
  try {
    const list = await fetch(apiPath("/drawings")).then((r) => (r.ok ? r.json() : []));
    drawings = Array.isArray(list) ? list : [];
    drawIdCounter = Math.max(0, ...drawings.map((d) => d.id || 0));
    selectedDrawingId = null;
    renderDrawings();
  } catch (err) {
    // keep what is shown
  }
}

async function applyLiveHistoryEntry(entry, direction) {
  const state = direction === "undo" ? entry.before : entry.after;
  if (entry.kind === "move") {
    for (const [name, pos] of Object.entries(state)) {
      if (nodesData[name]) { nodesData[name].x = pos.x; nodesData[name].y = pos.y; }
    }
    const existing = Object.fromEntries(Object.entries(state).filter(([name]) => nodesData[name]));
    for (const name of Object.keys(existing)) moveNodeShape(name);
    if (Object.keys(existing).length) await postPositions(existing);
  } else {
    drawings = cloneDrawings(state);
    drawIdCounter = Math.max(drawIdCounter, ...drawings.map((d) => d.id || 0));
    selectedDrawingId = null;
    renderDrawings();
    lastPersistedDrawings = cloneDrawings(state);
    try {
      await fetch(apiPath("/drawings"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ drawings }),
      });
    } catch (err) { /* local state is already right */ }
  }
}

async function undoRedo(direction) {
  if (designMode) {
    const resp = await fetch(`/design/${direction}`, { method: "POST" });
    if (!resp.ok) { await refreshHistoryState(); return; }
    deselectNode();
    await loadCanvas();
    await reloadDrawingsFromServer();
    setMenubarMessage(direction === "undo" ? "Undone." : "Redone.");
    return;
  }
  const from = direction === "undo" ? liveHistory.undo : liveHistory.redo;
  const to = direction === "undo" ? liveHistory.redo : liveHistory.undo;
  const entry = from.pop();
  if (!entry) return;
  await applyLiveHistoryEntry(entry, direction);
  to.push(entry);
  await refreshHistoryState();
  setMenubarMessage(direction === "undo" ? "Undone." : "Redone.");
}
const undoAction = () => undoRedo("undo");
const redoAction = () => undoRedo("redo");

document.getElementById("edit-undo").addEventListener("click", undoAction);
document.getElementById("edit-redo").addEventListener("click", redoAction);

// --- Copy / paste / duplicate (design mode only: pasting in a live lab would
// deploy real VMs). The clipboard holds the nodes' role/image, their
// positions and the links between the copied nodes; one /design/paste call
// adds everything (or nothing) as a single undo step. ---

let nodeClipboard = null;   // {nodes: [{name, role, image, x, y}], links: [{node1, if1, node2, if2}]}
let pasteCount = 0;         // each paste of the same copy lands a bit further along

function nextFreeName(base, taken) {
  const match = /^(.*?)(\d+)$/.exec(base);
  const prefix = match ? match[1] : `${base}-`;
  let n = match ? parseInt(match[2], 10) + 1 : 2;
  while (taken.has(`${prefix}${n}`) || nodesData[`${prefix}${n}`]) n++;
  return `${prefix}${n}`;
}

function copySelection() {
  if (!designMode) return false;
  const names = [...selectedNames].filter((n) => nodesData[n]);
  if (!names.length) return false;
  const inSet = new Set(names);
  const links = topologyLinks()
    .filter((l) => inSet.has(l.a) && inSet.has(l.b))
    .map((l) => ({ node1: l.a, if1: l.ifA, node2: l.b, if2: l.ifB }));
  nodeClipboard = {
    nodes: names.map((n) => ({ name: n, role: nodesData[n].role, image: nodesData[n].image, x: nodesData[n].x, y: nodesData[n].y })),
    links,
  };
  pasteCount = 0;
  setMenubarMessage(`Copied ${names.length} node${names.length === 1 ? "" : "s"}.`);
  return true;
}

async function pasteClipboard() {
  if (!designMode || !nodeClipboard) return;
  pasteCount++;
  const step = 40 * pasteCount;
  const taken = new Set();
  const renamed = {};
  for (const node of nodeClipboard.nodes) {
    renamed[node.name] = nextFreeName(node.name, taken);
    taken.add(renamed[node.name]);
  }
  const place = (v) => (snapToGridEnabled ? snapToGrid(v) : v);
  const body = {
    nodes: nodeClipboard.nodes.map((n) => ({
      name: renamed[n.name], role: n.role, image: n.image, x: place(n.x + step), y: place(n.y + step),
    })),
    links: nodeClipboard.links.map((l) => ({ node1: renamed[l.node1], if1: l.if1, node2: renamed[l.node2], if2: l.if2 })),
  };
  try {
    const resp = await fetch("/design/paste", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      await showAlert(`Couldn't paste: ${(await resp.json().catch(() => ({}))).detail || resp.status}`, { title: "Paste" });
      return;
    }
    await loadCanvas();
    setSelection(new Set(Object.values(renamed)), null);
    setMenubarMessage(`Pasted ${body.nodes.length} node${body.nodes.length === 1 ? "" : "s"}.`);
  } catch (err) {
    await showAlert(`Couldn't paste: ${err}`, { title: "Paste" });
  }
}

async function duplicateSelection() {
  if (copySelection()) await pasteClipboard();
}

function selectAllNodes() {
  setSelection(new Set(Object.keys(nodesData)), null);
}

function deleteSelection() {
  const names = [...selectedNames].filter((n) => nodesData[n]);
  if (names.length) deleteNodesMany(names);
}

function updateEditMenu() {
  const hasSelection = selectedNames.size > 0;
  for (const id of ["edit-copy", "edit-paste", "edit-duplicate"]) {
    document.getElementById(id).hidden = !designMode;
  }
  document.getElementById("edit-copy").disabled = !hasSelection;
  document.getElementById("edit-duplicate").disabled = !hasSelection;
  document.getElementById("edit-paste").disabled = !nodeClipboard;
  document.getElementById("edit-delete").disabled = !hasSelection;
  document.getElementById("edit-select-all").disabled = Object.keys(nodesData).length === 0;
  refreshHistoryState();
}
document.querySelector('.menu-btn[data-menu="edit"]').addEventListener("click", updateEditMenu);
document.getElementById("edit-copy").addEventListener("click", copySelection);
document.getElementById("edit-paste").addEventListener("click", pasteClipboard);
document.getElementById("edit-duplicate").addEventListener("click", duplicateSelection);
document.getElementById("edit-delete").addEventListener("click", deleteSelection);
document.getElementById("edit-select-all").addEventListener("click", selectAllNodes);
document.getElementById("multi-copy-btn").addEventListener("click", () => { copySelection(); });

// --- Find node ---

let finderMatches = [];
let finderIndex = 0;

function renderFinderResults() {
  const query = document.getElementById("node-finder-input").value.trim().toLowerCase();
  const list = document.getElementById("node-finder-results");
  list.replaceChildren();
  finderMatches = Object.values(nodesData)
    .filter((n) => !query || [n.name, n.role, n.image].some((v) => String(v || "").toLowerCase().includes(query)))
    .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
  finderIndex = Math.min(finderIndex, Math.max(0, finderMatches.length - 1));
  if (!finderMatches.length) {
    const li = document.createElement("li");
    li.className = "finder-empty";
    li.textContent = "No node matches.";
    list.appendChild(li);
    return;
  }
  finderMatches.forEach((node, index) => {
    const li = document.createElement("li");
    li.className = index === finderIndex ? "active" : "";
    const name = document.createElement("span");
    name.textContent = node.name;
    const meta = document.createElement("span");
    meta.className = "finder-meta";
    meta.textContent = [node.role, node.image].filter(Boolean).join(" · ");
    li.append(name, meta);
    li.addEventListener("click", () => pickFinderMatch(index));
    list.appendChild(li);
  });
}

function openNodeFinder() {
  const box = document.getElementById("node-finder");
  box.hidden = false;
  const input = document.getElementById("node-finder-input");
  input.value = "";
  finderIndex = 0;
  renderFinderResults();
  input.focus();
}

function closeNodeFinder() {
  document.getElementById("node-finder").hidden = true;
}

// Centres the view on the node (keeping the current zoom) and selects it.
function focusNode(name) {
  const node = nodesData[name];
  if (!node) return;
  viewBoxState.x = node.x - viewBoxState.w / 2;
  viewBoxState.y = node.y - viewBoxState.h / 2;
  applyViewBox();
  updateStatusBar(true);
  const p = svgToScreenPoint(node.x, node.y);
  selectNode(name, { x: p.x + 40, y: p.y });
}

function pickFinderMatch(index) {
  const node = finderMatches[index];
  if (!node) return;
  closeNodeFinder();
  focusNode(node.name);
}

document.getElementById("node-finder-input").addEventListener("input", () => { finderIndex = 0; renderFinderResults(); });
document.getElementById("node-finder-input").addEventListener("keydown", (evt) => {
  if (evt.key === "ArrowDown" || evt.key === "ArrowUp") {
    evt.preventDefault();
    if (!finderMatches.length) return;
    finderIndex = (finderIndex + (evt.key === "ArrowDown" ? 1 : -1) + finderMatches.length) % finderMatches.length;
    renderFinderResults();
    document.querySelector("#node-finder-results li.active")?.scrollIntoView({ block: "nearest" });
  } else if (evt.key === "Enter") {
    evt.preventDefault();
    pickFinderMatch(finderIndex);
  } else if (evt.key === "Escape") {
    evt.stopPropagation();
    closeNodeFinder();
  }
});
document.addEventListener("pointerdown", (evt) => {
  const box = document.getElementById("node-finder");
  if (!box.hidden && !box.contains(evt.target)) closeNodeFinder();
});
document.getElementById("edit-find").addEventListener("click", () => setTimeout(openNodeFinder, 0));

// --- Nodes menu: lifecycle for every node ---
for (const action of ["start", "stop", "reload"]) {
  document.getElementById(`nodes-${action}-all`).addEventListener("click", () => confirmAndRunMany(action, Object.keys(nodesData)));
}

// --- Keyboard shortcuts ---

const SHORTCUTS = [
  ["Ctrl+Z", "Undo"],
  ["Ctrl+Y / Ctrl+Shift+Z", "Redo"],
  ["Ctrl+A", "Select all nodes"],
  ["Ctrl+Click, Shift+Click", "Add or remove a node from the selection"],
  ["Shift+Drag on the canvas", "Rubber-band selection (Ctrl adds to it)"],
  ["Ctrl+C / Ctrl+V / Ctrl+D", "Copy / paste / duplicate nodes (design mode)"],
  ["Delete, Backspace", "Delete the selected nodes (or the selected drawing)"],
  ["Arrow keys", "Move the selection (Shift = 5x further)"],
  ["Ctrl+F", "Find a node"],
  ["+  /  -  /  0", "Zoom in / zoom out / fit to nodes"],
  ["Esc", "Cancel the current mode, close popups, clear the selection"],
  ["?", "This list"],
];
function openShortcutsDialog() {
  const table = document.getElementById("shortcuts-table");
  if (!table.children.length) {
    for (const [keys, what] of SHORTCUTS) {
      const row = table.insertRow();
      row.insertCell().textContent = keys;
      row.insertCell().textContent = what;
    }
  }
  document.getElementById("shortcuts-dialog").showModal();
}
document.getElementById("edit-shortcuts").addEventListener("click", openShortcutsDialog);
document.getElementById("shortcuts-close").addEventListener("click", () => document.getElementById("shortcuts-dialog").close());

let arrowMove = null; // {before, timer} - held arrow keys become one undo step
function moveSelectionBy(dx, dy) {
  const names = [...selectedNames].filter((n) => nodesData[n]);
  if (!names.length) return;
  if (!arrowMove) arrowMove = { before: Object.fromEntries(names.map((n) => [n, { x: nodesData[n].x, y: nodesData[n].y }])), timer: null };
  for (const n of names) {
    nodesData[n].x += dx;
    nodesData[n].y += dy;
    moveNodeShape(n);
  }
  clearTimeout(arrowMove.timer);
  arrowMove.timer = setTimeout(() => {
    const { before } = arrowMove;
    arrowMove = null;
    const after = Object.fromEntries(Object.keys(before).map((n) => [n, { x: nodesData[n].x, y: nodesData[n].y }]));
    commitMove(before, after);
  }, 500);
}

function typingInside(el) {
  return !!(el && el.closest && el.closest("input, textarea, select, [contenteditable='true'], .xterm"));
}

window.addEventListener("keydown", async (evt) => {
  if (document.getElementById("app-view").hidden) return;
  if (canvasLocked) return; // an import is running: no undo, delete or select-all on the old picture
  if (typingInside(evt.target)) return;
  if (document.querySelector("dialog[open]") || !document.getElementById("modal-overlay").hidden) return;
  if (svg.getClientRects().length === 0) return; // canvas not shown (Config Push / Logs tab)
  const mod = evt.ctrlKey || evt.metaKey;
  const key = evt.key.toLowerCase();
  const handled = () => evt.preventDefault();

  if (mod && key === "z") { handled(); await (evt.shiftKey ? redoAction() : undoAction()); return; }
  if (mod && key === "y") { handled(); await redoAction(); return; }
  if (mod && key === "a") { handled(); selectAllNodes(); return; }
  if (mod && key === "c") { if (designMode && selectedNames.size) { handled(); copySelection(); } return; }
  if (mod && key === "v") { if (designMode && nodeClipboard) { handled(); await pasteClipboard(); } return; }
  if (mod && key === "d") { handled(); if (designMode) await duplicateSelection(); return; }
  if (mod && key === "f") { handled(); openNodeFinder(); return; }
  if (mod || evt.altKey) return;

  if (key === "delete" || key === "backspace") {
    if (selectedNames.size) { handled(); deleteSelection(); }
    else if (selectedDrawingId !== null) {
      handled();
      drawings = drawings.filter((d) => d.id !== selectedDrawingId);
      selectedDrawingId = null;
      renderDrawings();
      persistDrawings();
    }
    return;
  }
  if (key.startsWith("arrow")) {
    if (!selectedNames.size) return;
    handled();
    const step = (snapToGridEnabled ? GRID_SIZE : 10) * (evt.shiftKey ? 5 : 1);
    moveSelectionBy(key === "arrowleft" ? -step : key === "arrowright" ? step : 0, key === "arrowup" ? -step : key === "arrowdown" ? step : 0);
    return;
  }
  const center = { x: viewBoxState.x + viewBoxState.w / 2, y: viewBoxState.y + viewBoxState.h / 2 };
  if (key === "+" || key === "=") { handled(); zoomBy(0.9, center); return; }
  if (key === "-" || key === "_") { handled(); zoomBy(1.1, center); return; }
  if (key === "0") { handled(); fitViewToNodes(); return; }
  if (key === "?" || key === "f1") { handled(); openShortcutsDialog(); }
});

// --- Actions on a multi-selection (and, for "Nodes > ... all", on every node) ---

async function lifecycleMany(action, names) {
  const live = names.filter((n) => nodesData[n] && nodesData[n].live);
  if (!live.length) return;
  const label = { start: "Starting", stop: "Stopping", reload: "Reloading" }[action];
  const failures = [];
  for (const [index, name] of live.entries()) {
    setMenubarProgress(`${label} nodes…`, index, live.length);
    try {
      const resp = await fetch(`/nodes/${encodeURIComponent(name)}/${action}`, { method: "POST" });
      if (!resp.ok) failures.push(`${name}: ${(await resp.json().catch(() => ({}))).detail || resp.status}`);
    } catch (err) {
      failures.push(`${name}: ${err}`);
    }
  }
  clearMenubarStatus();
  await loadCanvas();
  if (failures.length) {
    await showAlert(`${failures.length} of ${live.length} nodes failed to ${action}:\n${failures.join("\n")}`, { title: "Nodes" });
  } else {
    setMenubarMessage(`${label.replace("ing", "ed")} ${live.length} node${live.length === 1 ? "" : "s"}.`);
  }
}

async function confirmAndRunMany(action, names) {
  const live = names.filter((n) => nodesData[n] && nodesData[n].live);
  if (!live.length) { await showAlert("None of these nodes has a live instance.", { title: "Nodes" }); return; }
  if (action !== "start") {
    const verb = action === "stop" ? "Stop" : "Reload";
    const extra = action === "reload" ? " This restarts them from scratch." : "";
    if (!await showConfirm(`${verb} ${live.length} node${live.length === 1 ? "" : "s"}?${extra}`, { danger: true, confirmLabel: verb })) return;
  }
  await lifecycleMany(action, names);
}

async function deleteNodesMany(names) {
  if (!names.length) return;
  const count = `${names.length} node${names.length === 1 ? "" : "s"}`;
  const them = names.length === 1 ? "it" : "them";
  const message = designMode
    ? `Delete ${count}? This also removes any links to ${them}. (Ctrl+Z undoes it.)`
    : `Delete ${count}? This removes ${names.length === 1 ? "its" : "their"} live GNS3/ESXi instance${names.length === 1 ? "" : "s"} and any links to ${them} - it can't be undone.`;
  if (!await showConfirm(message, { danger: true, confirmLabel: "Delete" })) return;
  try {
    if (designMode) {
      const resp = await fetch("/design/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nodes: names }),
      });
      if (!resp.ok) { await showAlert(`Failed to delete: ${(await resp.json()).detail || resp.status}`); return; }
    } else {
      for (const [index, name] of names.entries()) {
        setMenubarProgress("Deleting nodes…", index, names.length);
        const resp = await fetch(`/nodes/${encodeURIComponent(name)}`, { method: "DELETE" });
        if (!resp.ok) {
          clearMenubarStatus();
          await showAlert(`Failed to delete "${name}": ${(await resp.json().catch(() => ({}))).detail || resp.status}\nStopped after ${index} of ${names.length}.`);
          break;
        }
        closeSessionsForNode(name);
      }
      clearMenubarStatus();
    }
    deselectNode();
    await loadCanvas();
  } catch (err) {
    clearMenubarStatus();
    await showAlert(`Failed to delete: ${err}`);
  }
}

document.getElementById("multi-start-btn").addEventListener("click", () => confirmAndRunMany("start", [...selectedNames]));
document.getElementById("multi-stop-btn").addEventListener("click", () => confirmAndRunMany("stop", [...selectedNames]));
document.getElementById("multi-reload-btn").addEventListener("click", () => confirmAndRunMany("reload", [...selectedNames]));
document.getElementById("multi-delete-btn").addEventListener("click", () => deleteNodesMany([...selectedNames]));

// Reproduces GNS3's own adapter-naming rule (see app/topology_write.py's
// _gns3_template_port_specs docstring for where port_name_format/
// port_segment_size/first_port_name come from and why this is safe to
// compute without ever deploying the node): adapter index i's name is
// port_name_format with {0}/{1} filled in from (port-within-segment,
// segment number) - or first_port_name verbatim for index 0, when the
// template gives one.
function computeGns3PortNames(spec) {
  const segSize = spec.port_segment_size || 0;
  const names = [];
  for (let i = 0; i < spec.adapters; i++) {
    if (i === 0 && spec.first_port_name) {
      names.push(spec.first_port_name);
      continue;
    }
    const portInSegment = segSize ? i % segSize : i;
    const segment = segSize ? Math.floor(i / segSize) : 0;
    names.push(spec.port_name_format.replaceAll("{0}", portInSegment).replaceAll("{1}", segment));
  }
  return names;
}

async function promptForInterface(nodeName, anchor) {
  // The Internet uplink has exactly one port - nothing to choose.
  if (nodesData[nodeName] && isUplinkImage(nodesData[nodeName].image)) {
    if (Object.keys(nodesData[nodeName].interfaces || {}).length) {
      await showAlert(`${nodeName} is an Internet uplink with a single port, and it is already connected.`, { title: "Interface" });
      return null;
    }
    return "nat0";
  }
  let ports = null;
  try {
    const resp = await fetch(apiPath(`/nodes/${encodeURIComponent(nodeName)}/ports`));
    if (resp.ok) ports = await resp.json();
  } catch (err) {
    // fall through to the fallbacks below
  }

  let options, defaultValue;
  if (ports) {
    // GNS3 only knows its own links; the topology file can hold one GNS3 lacks (or a design-only one). An
    // interface that either side uses is taken - the file rejects a second use of it ("used twice").
    const fileInterfaces = nodesData[nodeName]?.interfaces || {};
    const taken = (p) => p.in_use || !!fileInterfaces[p.name];
    const free = ports.filter((p) => !taken(p));
    options = ports.map((p) => ({
      value: p.name,
      label: `${p.name}${taken(p) ? " (in use)" : ""}`,
      disabled: taken(p),
    }));
    defaultValue = free.length ? free[0].name : undefined;
  } else {
    const node = nodesData[nodeName];
    const knownInterfaces = node?.interfaces || {};
    const templates = await ensureTemplatesLoaded();
    const spec = node ? templates.gns3_port_specs?.[node.image] : null;
    // Every port GNS3 would actually create for this image (computed,
    // never deployed - see computeGns3PortNames), with any already-
    // linked interface folded in and marked in-use even if it somehow
    // isn't one of the computed names (a link made before this image's
    // spec was available, or a spec that's since changed upstream).
    const names = spec
      ? Array.from(new Set([...computeGns3PortNames(spec), ...Object.keys(knownInterfaces)]))
      : Object.keys(knownInterfaces);
    if (!names.length) {
      return showTextPrompt(`Interface name on ${nodeName}:`, { title: "Interface" });
    }
    const inUse = (n) => knownInterfaces[n];
    const free = names.filter((n) => !inUse(n));
    options = names.map((n) => ({
      value: n,
      label: `${n}${inUse(n) ? " (in use)" : ""}`,
      disabled: !!inUse(n),
    }));
    defaultValue = free.length ? free[0] : undefined;
  }
  return showInlineDropdown(options, { title: `Interface on ${nodeName}`, anchor, defaultValue });
}

async function handleLinkModeClick(name, anchor) {
  if (!linkFirstNode) {
    // GNS3 Desktop's own link-drawing order: pick this node's port
    // right away, before the second node is even chosen - not
    // batched at the end like the old flow used to do.
    const if1 = await promptForInterface(name, anchor);
    if (!if1) { exitLinkMode(); return; }
    linkFirstNode = name;
    linkFirstIf = if1;
    document.getElementById("link-mode-hint").textContent =
      `Click the node to connect ${name}:${if1} to (Esc to cancel)`;
    return;
  }
  const node1 = linkFirstNode;
  const if1 = linkFirstIf;
  const node2 = name;
  linkFirstNode = null;
  linkFirstIf = null;
  if (node1 === node2) return;

  const if2 = await promptForInterface(node2, anchor);
  if (!if2) { exitLinkMode(); return; }
  if (!await confirmDisruption(
    `Connect ${node1}:${if1} <-> ${node2}:${if2}?\n\nThis briefly disrupts every ` +
    `already-running ESXi-bridged Cloud connection, even for just this one link.`,
    { title: "Add link", confirmLabel: "Connect" }
  )) {
    exitLinkMode();
    return;
  }
  createLink(node1, if1, node2, if2);
}

// A live link takes several seconds (the topology is redeployed); until it is drawn the menubar says what is going on, so
// the canvas not changing yet is not mistaken for "nothing happened".
async function createLink(node1, if1, node2, if2) {
  exitLinkMode();
  if (!designMode) setMenubarMessage(`Adding link ${node1}:${if1} <-> ${node2}:${if2}…`);
  try {
    const resp = await fetch(apiPath("/links"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node1, if1, node2, if2 }),
    });
    if (!resp.ok) {
      clearMenubarStatus();
      await showAlert(`Failed to add link: ${(await resp.json()).detail || resp.status}`);
      return;
    }
    await loadCanvas();
    clearMenubarStatus();
  } catch (err) {
    clearMenubarStatus();
    await showAlert(`Failed to add link: ${err}`);
  }
}

async function deleteLink(node1, if1, node2, if2) {
  if (!designMode) setMenubarMessage(`Deleting link ${node1}:${if1} <-> ${node2}:${if2}…`);
  try {
    const resp = await fetch(
      apiPath(`/links/${encodeURIComponent(node1)}/${encodeURIComponent(node2)}${linkQuery(if1, if2)}`),
      { method: "DELETE" }
    );
    if (!resp.ok) {
      clearMenubarStatus();
      await showAlert(`Failed to delete link: ${(await resp.json()).detail || resp.status}`);
      return;
    }
    await loadCanvas();
    clearMenubarStatus();
  } catch (err) {
    clearMenubarStatus();
    await showAlert(`Failed to delete link: ${err}`);
  }
}
