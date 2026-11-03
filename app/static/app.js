const SVG_NS = "http://www.w3.org/2000/svg";

// Multiple simultaneous console/screen sessions, shown as tabs next to
// each other (like a terminal's own tabs) instead of one replacing
// another. Each session keeps its own live connection - a WebSocket +
// xterm.js Terminal, or an RFB instance - running even while its tab
// isn't the active one: switching only toggles which panel is visible
// ([hidden]), nothing is ever torn down or recreated by a switch, so a
// background tab keeps receiving real data and is instantly current
// when switched back to.
const VM_SCREEN_MAX_CONSECUTIVE_FAILURES = 8;
let sessions = new Map();   // id -> session object, see ensureSessionPanel
let activeSessionId = null;

function sessionIdFor(nodeName, kind) {
  return `${kind}:${nodeName}`;
}

// Creates a session's tab + panel the first time id is seen, or just
// returns the existing one - callers check the returned session's own
// `.kind` to tell "brand new, still needs to be opened" (null) apart
// from "already open or opening" (set), so re-clicking a node's own
// Console/Screen button switches to its existing tab instead of
// starting a duplicate connection.
function ensureSessionPanel(id, nodeName, title) {
  let session = sessions.get(id);
  if (session) return session;

  const tab = document.createElement("button");
  tab.className = "session-tab";
  tab.type = "button";
  const label = document.createElement("span");
  label.className = "session-tab-label";
  label.textContent = title;
  // the connection state sits beside the name (a failure is coloured, the full text is the tooltip)
  const statusEl = document.createElement("span");
  statusEl.className = "session-tab-status";
  const close = document.createElement("span");
  close.className = "session-tab-close";
  close.textContent = "×";
  close.title = "Close";
  close.addEventListener("click", (evt) => { evt.stopPropagation(); closeSession(id); });
  tab.append(label, statusEl, close);
  tab.addEventListener("click", () => onSessionTabClick(id));
  tab.addEventListener("pointerdown", (evt) => startTabDrag(evt, id));
  document.getElementById("session-tabbar").appendChild(tab);
  document.getElementById("session-tabbar").hidden = false;

  const panel = document.createElement("div");
  panel.className = "session-panel"; // starts inactive (see the CSS - default is the :not(.active) rule) until switchToSession adds .active
  const body = document.createElement("div");
  body.className = "session-panel-body";
  panel.append(body);
  document.getElementById("session-panels").appendChild(panel);

  document.getElementById("console-placeholder").style.display = "none";

  session = {
    id, nodeName, title, kind: null, popout: null,
    tabEl: tab, panelEl: panel, statusEl, bodyEl: body,
    ws: null, term: null, rfb: null,
    screenWanted: false, reconnectAttempts: 0,
  };
  sessions.set(id, session);
  return session;
}

function setSessionStatus(session, text, dotClass) {
  session.statusEl.textContent = text;
  session.statusEl.title = text;
  session.statusEl.className = `session-tab-status ${dotClass || ""}`.trim();
}

function switchToSession(id) {
  activeSessionId = id;
  for (const s of sessions.values()) {
    // a session in its own window keeps its panel active (visible) there; its tab here just points to the window
    const popped = isPopped(s);
    const active = !popped && s.id === id;
    s.panelEl.classList.toggle("active", popped || active);
    s.tabEl.classList.toggle("active", active);
  }
}

function closeSession(id) {
  const session = sessions.get(id);
  if (!session) return;
  session.screenWanted = false;
  if (session.ws) session.ws.close();
  if (session.rfb) session.rfb.disconnect();
  const popout = session.popout;
  session.tabEl.remove();
  session.panelEl.remove();
  sessions.delete(id);
  if (popout && !popout.closed) popout.close();
  if (activeSessionId !== id) return;
  const next = [...sessions.values()].find((s) => !isPopped(s));
  if (next) {
    switchToSession(next.id);
  } else {
    activeSessionId = null;
    if (!sessions.size) document.getElementById("session-tabbar").hidden = true;
    document.getElementById("console-placeholder").style.display = "";
  }
}

// ---- tab drag-out (pure geometry, no DOM; tests/test_tab_dragout.py runs this block under node) ----
// Pressing a session tab and moving the pointer at least TAB_DRAG_THRESHOLD px is a drag (less is a click); a drag that is
// released more than TAB_DRAG_OUT_MARGIN px outside the Console/Screen area opens that session in its own window.
const TAB_DRAG_THRESHOLD = 8;
const TAB_DRAG_OUT_MARGIN = 24;
function tabDragState(start, point, area) {
  const dragging = Math.hypot(point.x - start.x, point.y - start.y) >= TAB_DRAG_THRESHOLD;
  const outside =
    point.x < area.left - TAB_DRAG_OUT_MARGIN ||
    point.x > area.right + TAB_DRAG_OUT_MARGIN ||
    point.y < area.top - TAB_DRAG_OUT_MARGIN ||
    point.y > area.bottom + TAB_DRAG_OUT_MARGIN;
  return { dragging, outside: dragging && outside };
}
// ---- end tab drag-out ----

function isPopped(session) {
  return !!(session.popout && !session.popout.closed);
}

let suppressTabClick = false; // the click that ends a drag must not also switch tabs

function onSessionTabClick(id) {
  if (suppressTabClick) return;
  const session = sessions.get(id);
  if (!session) return;
  if (isPopped(session)) {
    session.popout.focus();
    return;
  }
  switchToSession(id);
}

// Drag a tab out of the panel with the left mouse button to open just that session in its own window (the Pop out
// button does the same for the whole panel). Escape cancels the drag.
function startTabDrag(evt, id) {
  const session = sessions.get(id);
  if (!session || evt.button !== 0 || evt.target.closest(".session-tab-close")) return;
  if (isPopped(session) || (poppedOutWindow && !poppedOutWindow.closed)) return;
  const start = { x: evt.clientX, y: evt.clientY };
  const tab = session.tabEl;
  let hint = null;
  let state = { dragging: false, outside: false };
  let finished = false;
  const areaRect = () => document.getElementById("console-area").getBoundingClientRect();
  try {
    tab.setPointerCapture(evt.pointerId); // keeps the events coming while the pointer is outside the browser window
  } catch (err) {
    // not capturable: the window listeners below still work inside the page
  }

  const onMove = (e) => {
    state = tabDragState(start, { x: e.clientX, y: e.clientY }, areaRect());
    if (!state.dragging) return;
    if (!hint) {
      hint = document.createElement("div");
      hint.id = "tab-dragout-hint";
      document.body.appendChild(hint);
      tab.classList.add("dragging-out");
    }
    hint.textContent = state.outside ? "Release to open in its own window (Shift: new browser tab)" : "Drag out of the panel to open in its own window (Shift: new browser tab)";
    hint.classList.toggle("will-pop", state.outside);
    hint.style.left = `${Math.max(0, Math.min(e.clientX + 14, window.innerWidth - 300))}px`;
    hint.style.top = `${Math.max(0, Math.min(e.clientY + 14, window.innerHeight - 30))}px`;
  };
  const finish = (e, cancelled) => {
    if (finished) return;
    finished = true;
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    window.removeEventListener("pointercancel", onCancel);
    window.removeEventListener("keydown", onKey, true);
    if (hint) hint.remove();
    tab.classList.remove("dragging-out");
    try {
      tab.releasePointerCapture(evt.pointerId);
    } catch (err) {
      // already released
    }
    if (!cancelled && e.clientX !== undefined) state = tabDragState(start, { x: e.clientX, y: e.clientY }, areaRect());
    if (!state.dragging) return;
    suppressTabClick = true;
    setTimeout(() => { suppressTabClick = false; }, 0);
    if (!cancelled && state.outside) popOutSession(id, e.screenX, e.screenY, e.shiftKey);
  };
  const onUp = (e) => finish(e, false);
  const onCancel = (e) => finish(e, true);
  const onKey = (e) => { if (e.key === "Escape") finish(e, true); };
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp);
  window.addEventListener("pointercancel", onCancel);
  window.addEventListener("keydown", onKey, true);
}

// Opens one session in its own browser window by moving its live panel there (not a copy), so the terminal or screen and
// its connection keep running uninterrupted. Closing that window brings the session back into its tab.
// A pop-out opens as its own window; with `asTab` as a new browser tab instead (window.open without window features).
// Careful with the tab: the terminal and the VNC screen draw with requestAnimationFrame of THIS page, which a browser pauses
// while this page is a background tab - so a popped-out tab can show a frozen screen. A separate window has no such problem.
function openPopOut(asTab, features) {
  return asTab ? window.open("", "_blank") : window.open("", "_blank", features);
}

function popOutSession(id, screenX, screenY, asTab = false) {
  const session = sessions.get(id);
  if (!session) return;
  if (isPopped(session)) {
    session.popout.focus();
    return;
  }
  const width = 900;
  const height = 650;
  const win = openPopOut(
    asTab,
    `popup=yes,width=${width},height=${height},left=${Math.max(0, Math.round((screenX || 0) - width / 2))},top=${Math.max(0, Math.round((screenY || 0) - 20))}`,
  );
  if (!win) {
    showAlert("Couldn't open a new window or tab - check your browser's popup blocker.");
    return;
  }
  win.document.head.innerHTML = document.head.innerHTML; // first: it also carries this page's own <title>
  win.document.title = `Ginnungagap - ${session.title}`;
  // the same structure as in the panel, so every existing style applies to the moved session
  const area = win.document.createElement("div");
  area.id = "console-area";
  const panels = win.document.createElement("div");
  panels.id = "session-panels";
  area.appendChild(panels);
  win.document.body.appendChild(area);
  panels.appendChild(session.panelEl);
  session.panelEl.classList.add("active"); // visible in its own window, whichever tab was active here
  session.popout = win;
  session.tabEl.classList.add("popped");
  win.addEventListener("beforeunload", () => restoreSession(session, win));
  if (activeSessionId === id) {
    // the panel shows another session now, or the placeholder when there is none left
    const remaining = [...sessions.values()].find((s) => !isPopped(s));
    if (remaining) {
      switchToSession(remaining.id);
    } else {
      activeSessionId = null;
      document.getElementById("console-placeholder").style.display = "";
    }
  }
}

function restoreSession(session, win) {
  if (sessions.get(session.id) !== session || session.popout !== win) return;
  session.popout = null;
  session.tabEl.classList.remove("popped");
  document.getElementById("session-panels").appendChild(session.panelEl);
  document.getElementById("console-placeholder").style.display = "none";
  switchToSession(session.id);
}

window.addEventListener("beforeunload", () => {
  for (const s of sessions.values()) if (isPopped(s)) s.popout.close();
});

function closeSessionsForNode(nodeName) {
  for (const session of [...sessions.values()]) {
    if (session.nodeName === nodeName) closeSession(session.id);
  }
}

// In-app replacements for the browser's own confirm()/alert()/prompt(),
// styled to match the rest of the UI rather than popping an
// OS-chrome dialog. All resolve a Promise instead of blocking the
// thread, so every call site awaits them from an async function.
function openModal(config) {
  return new Promise((resolve) => {
    const overlay = document.getElementById("modal-overlay");
    const titleEl = document.getElementById("modal-title");
    const messageEl = document.getElementById("modal-message");
    const inputEl = document.getElementById("modal-input");
    const textareaEl = document.getElementById("modal-textarea");
    const fieldWrap = document.getElementById("modal-field-wrap");
    const confirmBtn = document.getElementById("modal-confirm-btn");
    const cancelBtn = document.getElementById("modal-cancel-btn");

    const isAlert = config.mode === "alert";
    const isConfirm = config.mode === "confirm";
    const isText = config.mode === "text";
    const isTextarea = config.mode === "textarea";

    titleEl.textContent = config.title || "";
    messageEl.textContent = config.message || "";
    inputEl.hidden = true;
    textareaEl.hidden = true;
    fieldWrap.hidden = !isText && !isTextarea;
    cancelBtn.hidden = isAlert;
    cancelBtn.textContent = config.cancelLabel || "Cancel";
    confirmBtn.textContent = config.confirmLabel || (isConfirm ? "Confirm" : "OK");
    confirmBtn.classList.toggle("danger", !!config.danger);
    confirmBtn.classList.toggle("primary", !config.danger);

    if (isText) {
      inputEl.hidden = false;
      inputEl.value = config.defaultValue || "";
      inputEl.placeholder = config.placeholder || "";
    } else if (isTextarea) {
      textareaEl.hidden = false;
      textareaEl.value = config.defaultValue || "";
      textareaEl.placeholder = config.placeholder || "";
    }

    function finish(result) {
      overlay.hidden = true;
      document.removeEventListener("keydown", onKeydown);
      confirmBtn.onclick = null;
      cancelBtn.onclick = null;
      overlay.onclick = null;
      resolve(result);
    }
    function doConfirm() {
      if (isAlert) return finish(undefined);
      if (isConfirm) return finish(true);
      if (isTextarea) return finish(textareaEl.value.trim() || null);
      return finish(inputEl.value.trim() || null);
    }
    function doCancel() {
      if (isAlert) return finish(undefined);
      if (isConfirm) return finish(false);
      return finish(null);
    }
    function onKeydown(evt) {
      if (evt.key === "Escape") { evt.preventDefault(); doCancel(); }
      // A plain Enter in the textarea types a newline instead of
      // submitting - Ctrl/Cmd+Enter submits it, same convention as
      // most chat/comment boxes. Every other mode still submits on a
      // plain Enter.
      else if (evt.key === "Enter" && isTextarea && !(evt.ctrlKey || evt.metaKey)) return;
      else if (evt.key === "Enter") { evt.preventDefault(); doConfirm(); }
    }

    confirmBtn.onclick = doConfirm;
    cancelBtn.onclick = doCancel;
    overlay.onclick = (evt) => { if (evt.target === overlay) doCancel(); };
    document.addEventListener("keydown", onKeydown);

    overlay.hidden = false;
    setTimeout(() => {
      const toFocus = isText ? inputEl : isTextarea ? textareaEl : confirmBtn;
      toFocus.focus();
      if (isText) inputEl.select();
    }, 0);
  });
}

function showAlert(message, opts = {}) {
  return openModal({ mode: "alert", title: opts.title || "Notice", message });
}
function showConfirm(message, opts = {}) {
  return openModal({
    mode: "confirm",
    title: opts.title || "Confirm",
    message,
    danger: opts.danger,
    confirmLabel: opts.confirmLabel,
    cancelLabel: opts.cancelLabel,
  });
}
function showTextPrompt(message, opts = {}) {
  return openModal({
    mode: "text",
    title: opts.title || "Enter value",
    message,
    defaultValue: opts.defaultValue,
    placeholder: opts.placeholder,
  });
}
function showTextareaPrompt(message, opts = {}) {
  return openModal({
    mode: "textarea",
    title: opts.title || "Enter value",
    message,
    defaultValue: opts.defaultValue,
    placeholder: opts.placeholder,
    confirmLabel: opts.confirmLabel,
  });
}

// A real dropdown, not a popup dialog: a small floating box (no dark
// overlay) positioned near opts.anchor ({x, y} in viewport coords),
// holding a native <select> that resolves the moment an option is
// picked - no separate OK button needed. Escape or a click outside it
// cancels (resolves null). Used for interface-picking during Add Link,
// which used to be a full modal (a <select> for GNS3 nodes with real
// live ports, or a blind free-text field for ones without).
// Positions a fixed-position floating element near a viewport-space
// anchor point ({x, y}, e.g. a click's clientX/clientY), clamped so it
// never runs off-screen. Shared by every floating popover in this
// app - the interface-picker dropdown, the node-actions popup.
function positionNear(el, anchor) {
  const a = anchor || { x: window.innerWidth / 2, y: window.innerHeight / 2 };
  const rect = el.getBoundingClientRect();
  const left = Math.max(8, Math.min(a.x + 12, window.innerWidth - rect.width - 12));
  const top = Math.max(8, Math.min(a.y + 12, window.innerHeight - rect.height - 12));
  el.style.left = `${left}px`;
  el.style.top = `${top}px`;
}

function showInlineDropdown(options, opts = {}) {
  return new Promise((resolve) => {
    const pop = document.createElement("div");
    pop.className = "inline-dropdown";

    if (opts.title) {
      const label = document.createElement("div");
      label.className = "inline-dropdown-title";
      label.textContent = opts.title;
      pop.appendChild(label);
    }

    const select = document.createElement("select");
    select.className = "inline-dropdown-select";
    for (const opt of options) {
      const o = document.createElement("option");
      o.value = opt.value;
      o.textContent = opt.label;
      if (opt.disabled) o.disabled = true;
      select.appendChild(o);
    }
    if (opts.defaultValue !== undefined) select.value = opts.defaultValue;
    pop.appendChild(select);

    const hint = document.createElement("div");
    hint.className = "inline-dropdown-hint";
    hint.textContent = "Enter to accept, Esc to cancel";
    pop.appendChild(hint);

    document.body.appendChild(pop);
    positionNear(pop, opts.anchor);

    function finish(result) {
      document.removeEventListener("mousedown", onOutside, true);
      document.removeEventListener("keydown", onKeydown, true);
      pop.remove();
      resolve(result);
    }
    function onOutside(evt) {
      if (!pop.contains(evt.target)) finish(null);
    }
    function onKeydown(evt) {
      if (evt.key === "Escape") { evt.preventDefault(); finish(null); }
      // Accept whatever's currently selected (the default, if the
      // operator never touched the dropdown at all) without forcing
      // them to actually change the selection first.
      else if (evt.key === "Enter") { evt.preventDefault(); finish(select.value); }
    }
    select.addEventListener("change", () => finish(select.value));
    // Deferred a tick - the same click that opened this dropdown (a
    // node click in link mode) is still bubbling as a mousedown right
    // now, and would otherwise immediately look like an "outside
    // click" and close what it just opened.
    setTimeout(() => document.addEventListener("mousedown", onOutside, true), 0);
    document.addEventListener("keydown", onKeydown, true);
    select.focus();
  });
}

const NODE_W = 70;
const NODE_H = 54;
const NODE_ICON_SIZE = 32;
const GRID_SIZE = 40; // matches #grid-pattern's own width/height below
let snapToGridEnabled = false;
function snapToGrid(value) {
  return Math.round(value / GRID_SIZE) * GRID_SIZE;
}

// Add node/link and project import all warn that the action briefly
// disrupts every already-running ESXi-bridged Cloud connection -
// genuinely true, but repetitive once an operator already knows it.
// Settings > Disruption warnings toggles whether that confirm still
// appears at all; when off, the action just proceeds directly.
let showDisruptionWarnings = true;
let showPortLabels = true;
// Offsets of the labels the user has moved, keyed "<node>|<interface>" (port label) or "<node>|" (the node's name);
// a label without an entry sits at its default spot. Loaded with the canvas, see saveLabelOffsets.
let labelOffsets = {};
// Free-text notes per node name (and the sticky-note mark on the canvas), see openNodeNote.
let nodeNotes = {};
// Reflects the server's own Settings.INCREMENTAL_DEPLOY (see /health),
// which only the server's settings.yml controls - not user-settable
// from here. Defaults true (matches the server's own default) until
// the first /health response updates it, so an early confirm dialog
// (unlikely in practice, but possible) describes the common case.
let incrementalDeploy = true;
// True while the canvas is showing the offline design sketch (see
// app/design.py) instead of a real, deployed host's live topology -
// reachable from the landing page's own "Design a Topology" tile.
// Nothing under /design/* ever deploys anything, so every write here
// is instant and never disruptive.
let designMode = false;
function apiPath(path) {
  return designMode ? `/design${path}` : path;
}

function setWarningBanner(text, opts = {}) {
  const banner = document.getElementById("warning-banner");
  const retryBtn = document.getElementById("warning-retry-btn");
  document.getElementById("warning-banner-text").textContent = text;
  banner.hidden = !text;
  retryBtn.hidden = !text || !opts.retryable;
  // Offered when the selected library project isn't deployed to GNS3 yet.
  warningDeployProject = text && opts.deployable ? opts.deployable : null;
  document.getElementById("warning-deploy-btn").hidden = !warningDeployProject;
  // Offered while the selected project is parked (see app/holds.py).
  warningHeldProject = opts.held ? opts.held : null;
  document.getElementById("warning-resume-btn").hidden = !warningHeldProject;
}
let warningDeployProject = null; // {id, name, filename}
let warningHeldProject = null;   // {id, name, host, held_at, ...}

document.getElementById("warning-retry-btn").addEventListener("click", async () => {
  const btn = document.getElementById("warning-retry-btn");
  btn.disabled = true;
  btn.textContent = "Retrying…";
  try {
    await fetch("/reload", { method: "POST" });
    await loadCanvas();
  } catch (err) {
    // loadCanvas's own catch already surfaces this - nothing extra to do here.
  } finally {
    btn.disabled = false;
    btn.textContent = "Retry";
  }
});
function confirmDisruption(message, opts) {
  if (designMode || !showDisruptionWarnings) return Promise.resolve(true);
  return showConfirm(message, opts);
}
// GNS3's own real "classic" icon set (GPL-2.0, by the GNS3 project -
// vendored unmodified from the live GNS3 controller's own
// /v2/symbols/:/symbols/classic/<name>.svg/raw, the exact same files
// GNS3's own desktop client renders) - replaces this app's earlier
// hand-drawn primitive-shape icons.
const ROLE_ICON_FILES = {
  ROUTER: "/vendor/gns3-icons/router.svg",
  SWITCH: "/vendor/gns3-icons/ethernet_switch.svg",
  PC: "/vendor/gns3-icons/computer.svg",
  FW: "/vendor/gns3-icons/firewall.svg",
  VM: "/vendor/gns3-icons/server.svg",
  UPLINK: "/vendor/gns3-icons/cloud.svg",
};

// GNS3's built-in "NAT" node is this app's Internet uplink: one port (nat0),
// DHCP + NAT through the GNS3 VM's own network. It is stored like any other
// node (image NAT, role FW - TopologyBuilder only knows five roles), shown
// with a cloud icon.
function isUplinkImage(image) {
  return String(image || "").trim().toLowerCase() === "nat";
}

let nodesData = {};      // name -> {name, role, environment, interfaces, live, x, y}
let selectedName = null;   // the one selected node while exactly one is selected (drives the single-node popup)
let selectedNames = new Set(); // every selected node (one or several)
let linkMode = false;
let linkFirstNode = null;
let linkFirstIf = null; // node1's own already-picked interface name, chosen right when it was clicked

// Draw/Write - GNS3 Desktop's own drawing-toolbar equivalent
// (rectangle/ellipse/line/text annotations on the canvas). Client-side
// only for now: kept in browser memory and rendered on its own SVG
// layer, not persisted to the topology file or synced with GNS3's own
// real /v2/projects/{id}/drawings API - a real implementation of that
// would need backend work in TopologyBuilder's GNS3Connection first.
let drawings = [];
let drawIdCounter = 0;
let drawMode = null; // 'rect' | 'ellipse' | 'line' | 'text' | null
let drawDraft = null; // {type, x1, y1, x2, y2} while dragging out a new shape
let selectedDrawingId = null; // which drawing shows resize handles right now
let editingDrawingId = null; // which text drawing has an inline canvas editor open right now (hidden from the real SVG render while so)
let availableTemplates = null; // {gns3: [...], esxi: [...]}, cached after first fetch
let initialFitDone = false;
let drawingsLoaded = false; // fetch /drawings once, not on every loadCanvas() refresh
let placementRole = null;   // armed device's resolved role, or null if nothing armed
let placementImage = null;  // armed device's exact template/image name

// Template-name -> role classifier. There's no real backend link
// between a template's name and its role (TopologyBuilder's own
// role/image validation treats them as fully independent - see
// TopologyFileValidation.add_node) - role is just an operator-chosen
// label. This is purely a frontend convenience to narrow the Image
// list and to guess Role from an already-chosen Image.
//
// Environment (which of the two /templates lists a name came from)
// settles VM outright, before anything else: every ESXi template
// deploys as a full VM regardless of what OS/software it runs
// (Environment.get_environment decides ON_ESXI purely by list
// membership - see TopologyBuilder/src/graph/environment.py), and the
// canvas already always renders an ON_ESXI node's icon as VM
// regardless of its stored role (see roleForIcon below) - so guessing
// FW/PC/etc. from an ESXi template's name (e.g. "pfSense", "Windows
// 10 Pro Education") would just contradict how the node actually
// displays once deployed.
//
// For GNS3 templates, prefer GNS3's own real category
// (`/templates`'s `gns3_categories`, straight from the Template-API's
// `category` field - see app/topology_write.py's
// _gns3_template_categories) over guessing from the name: this is what
// tells Cisco's "IOSv" (category "router") and "IOSvL2" (category
// "switch") apart correctly, where a name-only keyword match can't
// (both contain "iosv"). Keyword matching is only a fallback for a
// GNS3 template with no category data at all (the category-fetch
// failed server-side, or - like VPCS/Cloud/NAT - GNS3 categorizes it
// as generic "guest" rather than a specific network-device category);
// anything that still matches nothing is left unclassified (shown
// under every role filter) rather than mis-bucketed.
const GNS3_CATEGORY_TO_ROLE = {
  router: "ROUTER",
  switch: "SWITCH",
  multilayer_switch: "SWITCH",
  firewall: "FW",
  guard: "FW",
};
const ROLE_KEYWORDS = [
  ["FW", ["firewall", "pfsense", "opnsense", "fortigate", "fortinet", "sophos", "checkpoint", "palo alto", "asa", "srx"]],
  ["SWITCH", ["switch", "iosvl2", "ovs", "vswitch", "nx-os", "nxos", "catalyst"]],
  ["ROUTER", ["router", "iosv", "ios-xr", "iosxr", "csr1000", "csr", "iou", "vios", "mikrotik", "vyos", "frr", "quagga", "juniper"]],
  ["PC", ["vpcs", "workstation", "desktop", "windows", "win10", "win11", "winxp", "client"]],
];

function classifyTemplateRole(name, group) {
  if (group === "esxi") return "VM";
  if (isUplinkImage(name)) return "FW";
  const category = availableTemplates && availableTemplates.gns3_categories && availableTemplates.gns3_categories[name];
  const mappedFromCategory = category && GNS3_CATEGORY_TO_ROLE[category];
  if (mappedFromCategory) return mappedFromCategory;
  const lower = name.toLowerCase();
  for (const [role, keywords] of ROLE_KEYWORDS) {
    if (keywords.some((kw) => lower.includes(kw))) return role;
  }
  return null;
}

function isNodeRunning(node) {
  const status = node.live && node.live.status ? node.live.status : "none";
  return status === "started" || status === "poweredOn";
}

// Shared by the canvas node dot and the Topology Summary list's own
// dot, so both always agree: red once genuinely stopped, yellow while
// GNS3 reports a node mid-boot ("starting" - some GNS3 node types,
// e.g. qemu-backed ones, go through this transition instead of
// stopped -> started in one jump), green once running, and the
// original hollow/dim look only for "no live data at all" (a node
// this app can't currently get a real status for).
function statusClassFor(node) {
  if (!node.live) return "status-unknown";
  if (node.live.status === "starting") return "status-starting";
  return isNodeRunning(node) ? "status-ok" : "status-off";
}

function roleForIcon(node) {
  if (node.environment === "ON_ESXI") return "VM";
  return isUplinkImage(node.image) ? "UPLINK" : node.role;
}

// A single <image> referencing the real vendored GNS3 icon file for
// role, sized into a size x size box - preserveAspectRatio keeps each
// icon's own real proportions (they vary a lot: ethernet_switch.svg is
// wide/short, server.svg is tall/narrow) rather than stretching them
// to a forced square.
function buildRoleIconImage(role, size) {
  const href = ROLE_ICON_FILES[role] || ROLE_ICON_FILES.PC;
  const img = document.createElementNS(SVG_NS, "image");
  img.setAttributeNS("http://www.w3.org/1999/xlink", "href", href);
  img.setAttribute("href", href);
  img.setAttribute("width", size);
  img.setAttribute("height", size);
  img.setAttribute("preserveAspectRatio", "xMidYMid meet");
  return img;
}

function buildRoleIconStandalone(role, size) {
  const s = size || 18;
  const svgEl = document.createElementNS(SVG_NS, "svg");
  svgEl.setAttribute("viewBox", `0 0 ${s} ${s}`);
  svgEl.setAttribute("width", s);
  svgEl.setAttribute("height", s);
  svgEl.appendChild(buildRoleIconImage(role, s));
  return svgEl;
}

const DESIGN_HEALTH_STUB = {
  gns3_project_name: "Untitled design (not deployed)",
  esxi_ip: "", gns3_vm_ip: null, gns3_project_id: null, warning: null,
  incremental_deploy: true,
};

async function loadCanvas() {
  try {
    const [health, topology, nodesResult, positions] = designMode
      ? [DESIGN_HEALTH_STUB, await fetch(apiPath("/topology")).then(r => r.json()), null, await fetch(apiPath("/layout")).then(r => r.json())]
      : await Promise.all([
        fetch("/health").then(r => r.json()),
        // 503s while app.state.graph couldn't load (e.g. the template
        // service is down) - fall back to an empty canvas instead of
        // throwing, so the health.warning banner (set below) stays the
        // one thing the user sees instead of a generic "Failed to load".
        fetch("/topology").then(r => r.ok ? r.json() : []),
        fetch("/nodes").then(r => r.ok ? r.json() : null),
        fetch("/layout").then(r => r.ok ? r.json() : {}),
      ]);

    document.getElementById("project-meta").textContent = designMode
      ? (typeof currentDesignProject !== "undefined" && currentDesignProject
        ? currentDesignProject.name : "Unsaved design")
      : `${health.gns3_project_name} @ ${health.esxi_ip}`;
    setWarningBanner(health.warning || "", { retryable: !designMode, deployable: designMode ? null : health.deployable_project, held: designMode ? null : health.held_project });
    incrementalDeploy = health.incremental_deploy !== false;

    const gns3Link = document.getElementById("gns3-webui-link");
    const esxiLink = document.getElementById("esxi-webui-link");
    if (health.gns3_vm_ip) {
      gns3Link.href = health.gns3_project_id
        ? `http://${health.gns3_vm_ip}:${health.gns3_port}/static/web-ui/server/1/project/${health.gns3_project_id}`
        : `http://${health.gns3_vm_ip}:${health.gns3_port}/`;
      gns3Link.classList.remove("menu-item-disabled");
    } else {
      gns3Link.href = "#";
      gns3Link.classList.add("menu-item-disabled");
    }
    esxiLink.href = health.esxi_ip ? `https://${health.esxi_ip}/` : "#";

    labelOffsets = await fetch(apiPath("/labels")).then(r => r.ok ? r.json() : {}).catch(() => ({}));
    nodeNotes = await fetch(apiPath("/notes")).then(r => r.ok ? r.json() : {}).catch(() => ({}));
    const liveByName = new Map((nodesResult || []).map(n => [n.name, n]));
    nodesData = {};
    for (const node of topology) {
      const pos = positions[node.name] || { x: 100, y: 100 };
      nodesData[node.name] = { ...node, live: liveByName.get(node.name) || null, x: pos.x, y: pos.y };
    }
    selectedNames = new Set([...selectedNames].filter((n) => nodesData[n]));
    if (selectedName && !nodesData[selectedName]) selectedName = null;
    if (selectedNames.size < 2 && !document.getElementById("multi-actions").hidden) deselectNode();
    await refreshLiveLinks();
    renderCanvas();
    renderSummary();
    if (!initialFitDone) {
      initialFitDone = true;
      fitViewToNodes();
    }
    if (!drawingsLoaded) {
      drawingsLoaded = true;
      try {
        const serverDrawings = await fetch(apiPath("/drawings")).then((r) => (r.ok ? r.json() : []));
        if (Array.isArray(serverDrawings) && serverDrawings.length) {
          drawings = serverDrawings;
          drawIdCounter = Math.max(0, ...drawings.map((d) => d.id || 0));
          lastPersistedDrawings = cloneDrawings(drawings);
          renderDrawings();
        }
      } catch (err) {
        // best-effort - drawings just start empty if this fails
      }
    }
    updateStatusBar(true);
    refreshHistoryState();
  } catch (err) {
    updateStatusBar(false);
    throw err;
  }
}

function edgePoint(from, to) {
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  if (dx === 0 && dy === 0) return { x: from.x, y: from.y };
  const hw = NODE_ICON_SIZE / 2;
  const hh = NODE_ICON_SIZE / 2;
  const scale = Math.min(
    dx !== 0 ? hw / Math.abs(dx) : Infinity,
    dy !== 0 ? hh / Math.abs(dy) : Infinity
  );
  return { x: from.x + dx * scale, y: from.y + dy * scale };
}

// Two nodes can be linked more than once (over different interfaces). Their lines run side by side:
// the k-th of n links between a pair is shifted by (k - (n-1)/2) * LINK_SPACING, perpendicular to the
// line between the two icons, measured from the alphabetically first node of the pair so both ends
// of a link, its line and its labels always agree on which side it is on.
const LINK_SPACING = 16;

function shiftedEdgePoint(near, far, offset, nearIsFirst) {
  const p = edgePoint(near, far);
  if (!offset) return p;
  const dx = far.x - near.x;
  const dy = far.y - near.y;
  const len = Math.hypot(dx, dy) || 1;
  const sign = nearIsFirst ? 1 : -1;
  return { x: p.x + (-dy / len) * offset * sign, y: p.y + (dx / len) * offset * sign };
}

// A little further out from the link-endpoint dot, in the same
// direction away from the icon's own center - reads as "next to the
// icon" instead of sitting on top of it. Shared by the initial render
// and by onNodeDrag's own live repositioning, so both always agree.
function portLabelPoint(nearNode, farNode, offset = 0, nearIsFirst = true) {
  const p = shiftedEdgePoint(nearNode, farNode, offset, nearIsFirst);
  const dx = p.x - nearNode.x;
  const dy = p.y - nearNode.y;
  const len = Math.hypot(dx, dy) || 1;
  return { x: p.x + (dx / len) * 12, y: p.y + (dy / len) * 12 };
}

// The note dialog: the node's free-text note, plus the default login its template lists (read-only; the password
// stays hidden until "Show" is clicked). Values go in through textContent only.
function renderNodeLogins(container, info) {
  container.replaceChildren();
  const rows = [];
  for (const login of info.logins || []) {
    const row = document.createElement("div");
    row.className = "note-login";
    const label = document.createElement("span");
    label.className = "note-login-label";
    label.textContent = "Default login";
    const user = document.createElement("span");
    user.className = "note-login-user";
    user.textContent = login.user || "(no user listed)";
    row.append(label, user);
    if (login.password) {
      const secret = document.createElement("span");
      secret.className = "note-login-secret";
      secret.textContent = "••••••••";
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "toolbar-btn";
      toggle.textContent = "Show";
      toggle.addEventListener("click", () => {
        const hidden = toggle.textContent === "Show";
        secret.textContent = hidden ? login.password : "••••••••";
        toggle.textContent = hidden ? "Hide" : "Show";
      });
      row.append(secret, toggle);
    }
    rows.push(row);
  }
  if (info.usage) {
    const usage = document.createElement("div");
    usage.className = "note-login";
    const label = document.createElement("span");
    label.className = "note-login-label";
    label.textContent = "Template says";
    const text = document.createElement("span");
    text.className = "note-login-user";
    text.textContent = info.usage;
    usage.append(label, text);
    rows.push(usage);
  }
  if (info.error) {
    const err = document.createElement("div");
    err.className = "note-login note-login-error";
    err.textContent = `Default login unavailable: ${info.error}.`;
    rows.push(err);
  }
  container.append(...rows);
  container.hidden = !rows.length;
}

async function openNodeNote(name) {
  const dialog = document.getElementById("note-dialog");
  const text = document.getElementById("note-text");
  const status = document.getElementById("note-status");
  const logins = document.getElementById("note-logins");
  document.getElementById("note-title").textContent = `Note: ${name}`;
  text.value = nodeNotes[name] || "";
  status.textContent = "";
  logins.hidden = true;
  logins.replaceChildren();
  if (!dialog.open) dialog.showModal();
  text.focus();

  fetch(apiPath(`/nodes/${encodeURIComponent(name)}/credentials`))
    .then((r) => (r.ok ? r.json() : null))
    .then((info) => { if (info && dialog.open && document.getElementById("note-title").textContent === `Note: ${name}`) renderNodeLogins(logins, info); })
    .catch(() => {});

  const save = async () => {
    status.textContent = "Saving…";
    try {
      const resp = await fetch(apiPath(`/notes/${encodeURIComponent(name)}`), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text.value }),
      });
      if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || resp.status);
      nodeNotes = await resp.json();
      if (designMode) refreshHistoryState();
      dialog.close();
      renderCanvas();
      if (selectedName === name) showNodeActions(name);
    } catch (err) {
      status.textContent = `Couldn't save the note: ${err.message || err}`;
    }
  };
  document.getElementById("note-save").onclick = save;
  document.getElementById("note-cancel").onclick = () => dialog.close();
  text.onkeydown = (evt) => {
    if ((evt.ctrlKey || evt.metaKey) && evt.key === "Enter") { evt.preventDefault(); save(); }
  };
}

function labelKey(nodeName, ifName = "") {
  return `${nodeName}|${ifName}`;
}

// A port label's place: the default spot next to its endpoint dot plus whatever the user dragged it by.
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

// --- Color parsing/formatting - SVG's fill/stroke attributes accept
// CSS rgba() directly (SVG2 uses CSS Color syntax), so a drawing's
// stroke/fill is just stored as whatever CSS color string the field
// below produces - "#rrggbb" for old saved drawings (full opacity,
// backward compatible) or "rgba(r, g, b, a)" once touched through it.

function parseCssColor(str) {
  const fallback = { r: 124, g: 179, b: 66, a: 1 };
  if (!str || str === "none") return fallback;
  const hex = str.match(/^#([0-9a-f]{6})([0-9a-f]{2})?$/i);
  if (hex) {
    return {
      r: parseInt(hex[1].slice(0, 2), 16),
      g: parseInt(hex[1].slice(2, 4), 16),
      b: parseInt(hex[1].slice(4, 6), 16),
      a: hex[2] ? parseInt(hex[2], 16) / 255 : 1,
    };
  }
  const rgba = str.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)/i);
  if (rgba) {
    return {
      r: Math.min(255, +rgba[1]), g: Math.min(255, +rgba[2]), b: Math.min(255, +rgba[3]),
      a: rgba[4] !== undefined ? Math.max(0, Math.min(1, +rgba[4])) : 1,
    };
  }
  return fallback;
}
function rgbaToCss({ r, g, b, a }) {
  return `rgba(${r}, ${g}, ${b}, ${Math.round(a * 100) / 100})`;
}
function rgbaToHex6({ r, g, b }) {
  const h = (n) => n.toString(16).padStart(2, "0");
  return `#${h(r)}${h(g)}${h(b)}`;
}

// Builds a checkerboard swatch button + popover (native hue/sat picker,
// an alpha slider, a live rgba()/hex text field) for a stroke/fill
// color. `initial` is any CSS color string (or "none"/falsy for a
// field that starts unset); onChange(cssColor) fires on every live
// edit (drag/type), same "input vs change" live-preview-then-persist
// split the rest of this popup's fields already use - the caller
// decides when to persistDrawings().
function buildColorField(initial, onChange) {
  const wrap = document.createElement("div");
  wrap.className = "color-field";
  let current = parseCssColor(initial);

  const swatchBtn = document.createElement("button");
  swatchBtn.type = "button";
  swatchBtn.className = "color-swatch-btn";
  const swatchFill = document.createElement("div");
  swatchFill.className = "color-swatch-fill";
  swatchBtn.appendChild(swatchFill);
  wrap.appendChild(swatchBtn);

  let pop = null;
  function syncSwatch() {
    swatchFill.style.background = rgbaToCss(current);
  }
  syncSwatch();

  function emit() {
    syncSwatch();
    onChange(rgbaToCss(current));
  }

  function closePopover() {
    if (!pop) return;
    document.removeEventListener("mousedown", onOutside, true);
    pop.remove();
    pop = null;
  }
  function onOutside(evt) {
    if (!wrap.contains(evt.target)) closePopover();
  }

  function openPopover() {
    if (pop) return;
    pop = document.createElement("div");
    pop.className = "color-popover";

    const hueRow = document.createElement("div");
    hueRow.className = "color-popover-row";
    const hueInput = document.createElement("input");
    hueInput.type = "color";
    hueInput.value = rgbaToHex6(current);
    hueInput.addEventListener("input", () => {
      const rgb = parseCssColor(hueInput.value);
      current = { ...rgb, a: current.a };
      textInput.value = rgbaToCss(current);
      emit();
    });
    hueRow.appendChild(hueInput);
    const textInput = document.createElement("input");
    textInput.type = "text";
    textInput.className = "color-text-input";
    textInput.value = rgbaToCss(current);
    textInput.spellcheck = false;
    function applyText() {
      current = parseCssColor(textInput.value);
      hueInput.value = rgbaToHex6(current);
      alphaSlider.value = Math.round(current.a * 100);
      alphaReadout.textContent = `${alphaSlider.value}%`;
      emit();
    }
    textInput.addEventListener("change", applyText);
    textInput.addEventListener("keydown", (evt) => {
      if (evt.key === "Enter") { evt.preventDefault(); applyText(); }
    });
    hueRow.appendChild(textInput);
    pop.appendChild(hueRow);

    const alphaRow = document.createElement("div");
    alphaRow.className = "color-popover-row";
    const alphaSlider = document.createElement("input");
    alphaSlider.type = "range";
    alphaSlider.className = "color-alpha-slider";
    alphaSlider.min = "0";
    alphaSlider.max = "100";
    alphaSlider.value = Math.round(current.a * 100);
    const alphaReadout = document.createElement("span");
    alphaReadout.className = "color-alpha-readout";
    alphaReadout.textContent = `${alphaSlider.value}%`;
    alphaSlider.addEventListener("input", () => {
      current = { ...current, a: +alphaSlider.value / 100 };
      textInput.value = rgbaToCss(current);
      alphaReadout.textContent = `${alphaSlider.value}%`;
      emit();
    });
    alphaRow.appendChild(alphaSlider);
    alphaRow.appendChild(alphaReadout);
    pop.appendChild(alphaRow);

    wrap.appendChild(pop);
    setTimeout(() => document.addEventListener("mousedown", onOutside, true), 0);
  }

  swatchBtn.addEventListener("click", (evt) => {
    evt.stopPropagation();
    if (pop) closePopover(); else openPopover();
  });

  wrap.closeColorPopover = closePopover;
  return wrap;
}

let closeOpenDrawingPopup = null;

function showDrawingActionsPopup(id, anchor) {
  if (closeOpenDrawingPopup) closeOpenDrawingPopup();
  const d = drawings.find((x) => x.id === id);
  if (!d) return;

  const pop = document.createElement("div");
  pop.className = "inline-dropdown drawing-popup";
  const label = document.createElement("div");
  label.className = "inline-dropdown-title";
  label.textContent = "Drawing";
  pop.appendChild(label);

  function addRow(labelText) {
    const row = document.createElement("div");
    row.className = "drawing-style-row";
    const l = document.createElement("label");
    l.textContent = labelText;
    row.appendChild(l);
    pop.appendChild(row);
    return row;
  }

  // Images have no stroke/fill/width of their own - just a position and
  // size, both already handled by the generic drag-to-move/resize-
  // handle code every other type also uses.
  if (d.type !== "image") {
    const colorField = buildColorField(d.stroke || (d.type === "text" ? "#1f2328" : "#7cb342"), (css) => {
      d.stroke = css;
      renderDrawings();
      persistDrawings();
    });
    addRow(d.type === "text" ? "Color" : "Stroke").appendChild(colorField);
  }

  if (d.type === "rect") {
    const radiusInput = document.createElement("input");
    radiusInput.type = "number";
    radiusInput.min = "0";
    radiusInput.max = "100";
    radiusInput.value = d.rx || 0;
    radiusInput.addEventListener("change", () => {
      d.rx = parseFloat(radiusInput.value) || 0;
      renderDrawings();
      persistDrawings();
    });
    addRow("Corners").appendChild(radiusInput);
  }

  if (d.type === "rect" || d.type === "ellipse") {
    const fillRow = addRow("Fill");
    const fillField = buildColorField(d.fill && d.fill !== "none" ? d.fill : "rgba(124, 179, 66, 0.18)", (css) => {
      d.fill = css;
      renderDrawings();
      persistDrawings();
    });
    fillRow.appendChild(fillField);
    const noFillBtn = document.createElement("button");
    noFillBtn.className = "drawing-style-clear";
    noFillBtn.textContent = "None";
    noFillBtn.title = "No fill";
    noFillBtn.addEventListener("click", () => {
      d.fill = "none";
      renderDrawings();
      persistDrawings();
    });
    fillRow.appendChild(noFillBtn);
  }

  if (d.type === "text") {
    const sizeInput = document.createElement("input");
    sizeInput.type = "number";
    sizeInput.min = "8";
    sizeInput.max = "72";
    sizeInput.value = d.fontSize || 16;
    sizeInput.addEventListener("change", () => {
      d.fontSize = parseFloat(sizeInput.value) || 16;
      renderDrawings();
      persistDrawings();
    });
    addRow("Size").appendChild(sizeInput);
  } else if (d.type !== "image") {
    const widthInput = document.createElement("input");
    widthInput.type = "number";
    widthInput.min = "1";
    widthInput.max = "20";
    widthInput.value = d.strokeWidth || 2;
    widthInput.addEventListener("change", () => {
      d.strokeWidth = parseFloat(widthInput.value) || 2;
      renderDrawings();
      persistDrawings();
    });
    addRow("Width").appendChild(widthInput);
  }

  const rotationInput = document.createElement("input");
  rotationInput.type = "number";
  rotationInput.min = "0";
  rotationInput.max = "359";
  rotationInput.value = Math.round(d.rotation || 0);
  rotationInput.addEventListener("change", () => {
    d.rotation = ((parseFloat(rotationInput.value) || 0) % 360 + 360) % 360;
    renderDrawings();
    persistDrawings();
  });
  addRow("Rotate").appendChild(rotationInput);

  if (d.type === "text") {
    const editTextBtn = document.createElement("button");
    editTextBtn.className = "menu-item";
    editTextBtn.textContent = "Edit text";
    editTextBtn.addEventListener("click", () => {
      close();
      editTextDrawingInline(d, { isNew: false });
    });
    pop.appendChild(editTextBtn);
  }

  // Layers - like GNS3's own "Bring to front"/"Send to back": drawings
  // only ever stack among themselves (the whole #drawings-layer always
  // sits behind every link/node regardless), so reordering is just
  // moving this entry within the plain `drawings` array - SVG paints
  // in document order, and renderDrawings() emits them in array order.
  const layerRow = document.createElement("div");
  layerRow.className = "drawing-layer-row";
  const toBackBtn = document.createElement("button");
  toBackBtn.className = "menu-item";
  toBackBtn.textContent = "Send to back";
  toBackBtn.addEventListener("click", () => {
    drawings = [d, ...drawings.filter((x) => x.id !== id)];
    renderDrawings();
    persistDrawings();
  });
  const toFrontBtn = document.createElement("button");
  toFrontBtn.className = "menu-item";
  toFrontBtn.textContent = "Bring to front";
  toFrontBtn.addEventListener("click", () => {
    drawings = [...drawings.filter((x) => x.id !== id), d];
    renderDrawings();
    persistDrawings();
  });
  layerRow.appendChild(toBackBtn);
  layerRow.appendChild(toFrontBtn);
  pop.appendChild(layerRow);

  const del = document.createElement("button");
  del.className = "menu-item drawing-delete-btn";
  del.textContent = "Delete";
  pop.appendChild(del);

  document.body.appendChild(pop);
  positionNear(pop, anchor);

  function close() {
    document.removeEventListener("mousedown", onOutside, true);
    pop.remove();
    closeOpenDrawingPopup = null;
    if (selectedDrawingId === id) {
      selectedDrawingId = null;
      renderDrawings();
    }
  }
  function onOutside(evt) {
    if (!pop.contains(evt.target)) close();
  }
  del.addEventListener("click", () => {
    drawings = drawings.filter((x) => x.id !== id);
    renderDrawings();
    persistDrawings();
    close();
  });
  closeOpenDrawingPopup = close;
  setTimeout(() => document.addEventListener("mousedown", onOutside, true), 0);
}

function renderCanvas() {
  const linksLayer = document.getElementById("links-layer");
  const nodesLayer = document.getElementById("nodes-layer");
  linksLayer.innerHTML = "";
  nodesLayer.innerHTML = "";
  document.getElementById("labels-layer").innerHTML = "";

  const links = topologyLinks();
  const perPair = {};
  for (const link of links) (perPair[linkPairKey(link.a, link.b)] ||= []).push(link);
  for (const link of links) {
    const pairKey = linkPairKey(link.a, link.b);
    const siblings = perPair[pairKey];
    const offset = (siblings.indexOf(link) - (siblings.length - 1) / 2) * LINK_SPACING;
    const [first, second] = pairKey.split("|");
    const flip = link.a !== first; // the offset is measured from the alphabetically first node
    const node = nodesData[link.a];
    const neighbour = nodesData[link.b];
    const ifName = link.ifA;
    const neighbourIfName = link.ifB;
    const liveLink = liveLinkFor(link.a, link.ifA, link.b, link.ifB);
    const isCapturing = !!(liveLink && liveLink.capturing);
    const impaired = !!(liveLink && (liveLink.delay_ms || liveLink.loss_percent || liveLink.corrupt_percent));

    const line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("class", `link-line${isCapturing ? " link-line-capturing" : ""}${impaired ? " link-line-impaired" : ""}${liveLink && liveLink.suspended ? " link-line-suspended" : ""}`);
    // The drawn line is only ~1.5 px wide, too thin to hit with a mouse: an invisible wide line
    // right before it takes the click (and the hover highlight, see styles.css).
    const hit = document.createElementNS(SVG_NS, "line");
    hit.setAttribute("class", "link-hit");
    for (const el of [line, hit]) {
      el.dataset.pair = pairKey;
      el.dataset.a = first;
      el.dataset.b = second;
      el.dataset.offset = offset;
      el.dataset.link = linkKey(link.a, link.ifA, link.b, link.ifB);
      setLineEndpoints(el, nodesData[first], nodesData[second], offset);
    }
    // which two port labels belong to this link (placePortLabels lets a label lie on its own line)
    line.dataset.endA = labelKey(link.a, link.ifA);
    line.dataset.endB = labelKey(link.b, link.ifB);
    const openLinkActions = (evt) => {
      evt.stopPropagation();
      if (linkMode) return;
      showLinkActions(link.a, link.ifA, link.b, link.ifB, { x: evt.clientX, y: evt.clientY });
    };
    hit.addEventListener("pointerdown", openLinkActions);
    line.addEventListener("pointerdown", openLinkActions);
    linksLayer.appendChild(hit);
    linksLayer.appendChild(line);
    appendLinkEndpointDot(linksLayer, node, neighbour, ifName, offset, !flip);
    appendLinkEndpointDot(linksLayer, neighbour, node, neighbourIfName, offset, flip);
  }

  for (const node of Object.values(nodesData)) {
    nodesLayer.appendChild(buildNodeShape(node));
  }
  layoutPortLabels();
}

function setLineEndpoints(line, a, b, offset = 0) {
  const pa = shiftedEdgePoint(a, b, offset, true);
  const pb = shiftedEdgePoint(b, a, offset, false);
  line.setAttribute("x1", pa.x);
  line.setAttribute("y1", pa.y);
  line.setAttribute("x2", pb.x);
  line.setAttribute("y2", pb.y);
}

// A small sticky note at the icon's top-left corner: this node has a note.
function buildNoteMark(iconX, iconY) {
  const mark = document.createElementNS(SVG_NS, "g");
  mark.setAttribute("class", "node-note-mark");
  mark.setAttribute("transform", `translate(${iconX - 7}, ${iconY - 5})`);
  const page = document.createElementNS(SVG_NS, "rect");
  page.setAttribute("width", 10);
  page.setAttribute("height", 12);
  page.setAttribute("rx", 1.5);
  mark.appendChild(page);
  for (const y of [4, 7]) {
    const line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("x1", 2);
    line.setAttribute("x2", 8);
    line.setAttribute("y1", y);
    line.setAttribute("y2", y);
    mark.appendChild(line);
  }
  return mark;
}

function buildNodeShape(node) {
  const g = document.createElementNS(SVG_NS, "g");
  g.setAttribute("class", `node-shape${selectedNames.has(node.name) ? " selected" : ""}`);
  g.setAttribute("transform", `translate(${node.x - NODE_W / 2}, ${node.y - NODE_ICON_SIZE / 2})`);
  g.dataset.name = node.name;

  const iconX = (NODE_W - NODE_ICON_SIZE) / 2;
  const iconY = 0;

  const selectBox = document.createElementNS(SVG_NS, "rect");
  selectBox.setAttribute("class", "node-select-box");
  selectBox.setAttribute("x", iconX - 4);
  selectBox.setAttribute("y", iconY - 4);
  selectBox.setAttribute("width", NODE_ICON_SIZE + 8);
  selectBox.setAttribute("height", NODE_ICON_SIZE + 8);
  selectBox.setAttribute("rx", 4);
  g.appendChild(selectBox);

  const icon = buildRoleIconImage(roleForIcon(node), NODE_ICON_SIZE);
  icon.setAttribute("x", iconX);
  icon.setAttribute("y", iconY);
  g.appendChild(icon);

  if (nodeNotes[node.name]) g.appendChild(buildNoteMark(iconX, iconY));

  const statusDot = document.createElementNS(SVG_NS, "circle");
  statusDot.setAttribute("class", `status-dot ${statusClassFor(node)}`);
  statusDot.setAttribute("cx", iconX + NODE_ICON_SIZE - 2);
  statusDot.setAttribute("cy", iconY + NODE_ICON_SIZE - 2);
  statusDot.setAttribute("r", 5);
  g.appendChild(statusDot);

  const label = document.createElementNS(SVG_NS, "text");
  label.setAttribute("x", NODE_W / 2);
  label.setAttribute("y", NODE_ICON_SIZE + 16);
  label.setAttribute("text-anchor", "middle");
  label.setAttribute("class", "node-name-label");
  label.textContent = node.name;
  applyNodeLabelOffset(label, node.name);
  const moveNameLabel = () => {
    applyNodeLabelOffset(label, node.name);
    scheduleLabelLayout(); // the name is an obstacle for the port labels
  };
  label.addEventListener("pointerdown", (evt) => startLabelDrag(evt, labelKey(node.name), moveNameLabel));
  label.addEventListener("dblclick", () => resetLabel(labelKey(node.name), moveNameLabel));
  g.appendChild(label);

  g.addEventListener("pointerdown", (evt) => startNodeDrag(evt, node.name));
  return g;
}

function renderSummary() {
  const list = document.getElementById("topology-summary-list");
  list.innerHTML = "";
  const names = Object.keys(nodesData).sort();

  if (!names.length) {
    const li = document.createElement("li");
    li.className = "summary-empty";
    li.textContent = "No nodes in this topology.";
    list.appendChild(li);
    return;
  }

  for (const name of names) {
    const node = nodesData[name];
    const statusText = node.live && node.live.status ? node.live.status : "no live data";

    const li = document.createElement("li");
    li.className = `summary-row${selectedNames.has(name) ? " selected" : ""}`;

    const dot = document.createElement("span");
    dot.className = `summary-dot ${statusClassFor(node)}`;

    const iconWrap = document.createElement("span");
    iconWrap.className = "summary-icon";
    iconWrap.appendChild(buildRoleIconStandalone(roleForIcon(node)));

    const nameEl = document.createElement("span");
    nameEl.className = "summary-name";
    nameEl.textContent = name;

    const statusEl = document.createElement("span");
    statusEl.className = "summary-status";
    statusEl.textContent = statusText;

    li.append(dot, iconWrap, nameEl, statusEl);
    li.addEventListener("click", (evt) => {
      const r = li.getBoundingClientRect();
      const anchor = { x: r.left, y: r.top + r.height / 2 };
      if (evt.ctrlKey || evt.metaKey || evt.shiftKey) toggleNodeSelection(name, anchor);
      else selectNode(name, anchor);
    });
    list.appendChild(li);
  }
}

let statusConnectionOk = true;

function updateStatusBar(connectionOk = statusConnectionOk) {
  statusConnectionOk = connectionOk;
  const nodes = Object.values(nodesData);
  const running = nodes.filter(isNodeRunning).length;
  const text = document.getElementById("status-connection-text");
  const parts = [`${nodes.length} node${nodes.length === 1 ? "" : "s"}`];
  if (!designMode) parts.push(`${running} running`);
  const links = Object.values(liveLinksData);
  const capturing = designMode ? 0 : links.filter((l) => l.capturing).length;
  const impaired = designMode ? 0 : links.filter((l) => l.suspended || l.delay_ms || l.loss_percent || l.corrupt_percent).length;
  if (capturing) parts.push(`${capturing} capturing`);
  if (impaired) parts.push(`${impaired} link${impaired === 1 ? "" : "s"} with conditions`);
  if (selectedNames.size > 1) parts.push(`${selectedNames.size} selected`);
  text.textContent = connectionOk
    ? `${designMode ? "Design" : "Connected"} · ${parts.join(", ")}`
    : "Disconnected - failed to reach the server";
  const zoomPercent = Math.round((1600 / viewBoxState.w) * 100);
  document.getElementById("zoom-readout").textContent = `${zoomPercent}%`;
  const zoomInput = document.getElementById("settings-zoom-input");
  if (document.activeElement !== zoomInput) zoomInput.value = zoomPercent;
}

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

// --- Link-actions popup (delete, and - for a live, non-design link -
// Wireshark-style packet capture, matching GNS3's own web GUI. See
// app/capture.py: capture start/stop/stream all proxy straight through
// to GNS3's own controller API for the real link this node-name pair
// resolves to. ---

let liveLinksData = {};     // linkKey -> live link (GNS3 port names)
let liveLinksByPair = {};   // node pair -> [live links]
let selectedLinkPair = null;

function linkPairKey(a, b) {
  return [a, b].sort().join("|");
}

// One link = (node, interface) on both ends. Two nodes can be linked more than once, so a link is
// never identified by the node pair alone.
function linkKey(a, ifA, b, ifB) {
  const x = `${a}:${ifA || ""}`;
  const y = `${b}:${ifB || ""}`;
  return x < y ? `${x}|${y}` : `${y}|${x}`;
}

// Every link of the topology once, as {a, ifA, b, ifB}.
function topologyLinks() {
  const links = [];
  const seen = new Set();
  const add = (a, ifA, b, ifB) => {
    if (!nodesData[a] || !nodesData[b]) return;
    const key = linkKey(a, ifA, b, ifB);
    if (seen.has(key)) return;
    seen.add(key);
    links.push({ a, ifA, b, ifB });
  };
  for (const node of Object.values(nodesData)) {
    if (Array.isArray(node.links)) {
      for (const l of node.links) add(node.name, l.interface, l.peer, l.peer_interface);
    } else {
      // a server without per-link data: at most one link per neighbour can be told apart
      for (const [ifName, neighbourName] of Object.entries(node.interfaces || {})) {
        if (!neighbourName || !nodesData[neighbourName]) continue;
        const back = Object.entries(nodesData[neighbourName].interfaces || {}).find(([, n]) => n === node.name);
        add(node.name, ifName, neighbourName, back ? back[0] : "");
      }
    }
  }
  return links;
}

// The live GNS3 link behind a topology link: by both interfaces, or - when the two nodes have just one
// live link between them (its port name may differ from the interface name, e.g. a VM's Cloud node) -
// that one.
function liveLinkFor(node1, if1, node2, if2) {
  const exact = liveLinksData[linkKey(node1, if1, node2, if2)];
  if (exact) return exact;
  const sameNodes = liveLinksByPair[linkPairKey(node1, node2)] || [];
  return sameNodes.length === 1 ? sameNodes[0] : null;
}

// ?if1=..&if2=.. for the endpoints that address one link of a pair
function linkQuery(if1, if2) {
  const params = new URLSearchParams();
  if (if1) params.set("if1", if1);
  if (if2) params.set("if2", if2);
  const text = params.toString();
  return text ? `?${text}` : "";
}

async function refreshLiveLinks() {
  if (designMode) {
    liveLinksData = {};
    liveLinksByPair = {};
    return;
  }
  try {
    const links = await fetch("/links").then((r) => (r.ok ? r.json() : []));
    liveLinksData = {};
    liveLinksByPair = {};
    for (const link of links) {
      liveLinksData[linkKey(link.node1, link.port1 || link.if1, link.node2, link.port2 || link.if2)] = link;
      (liveLinksByPair[linkPairKey(link.node1, link.node2)] ||= []).push(link);
    }
  } catch (err) {
    liveLinksData = {};
    liveLinksByPair = {};
  }
  updateStatusBar();
}

function renderLinkActionsPopup(node1, if1, node2, if2) {
  document.getElementById("link-actions-header").textContent =
    `${node1}:${if1 || "?"} ⇄ ${node2}:${if2 || "?"}`;

  const startBtn = document.getElementById("link-start-capture-btn");
  const stopBtn = document.getElementById("link-stop-capture-btn");
  const wsBtn = document.getElementById("link-wireshark-btn");

  const optionsBox = document.getElementById("link-options");
  if (designMode) {
    startBtn.hidden = true;
    stopBtn.hidden = true;
    wsBtn.hidden = true;
    optionsBox.hidden = true;
    return;
  }

  const live = liveLinkFor(node1, if1, node2, if2);
  optionsBox.hidden = !live;
  if (live) {
    document.getElementById("lo-delay").value = live.delay_ms || 0;
    document.getElementById("lo-jitter").value = live.jitter_ms || 0;
    document.getElementById("lo-loss").value = live.loss_percent || 0;
    document.getElementById("lo-corrupt").value = live.corrupt_percent || 0;
    document.getElementById("lo-suspend").checked = !!live.suspended;
    document.getElementById("link-options-note").textContent = (live.other_filters || []).length
      ? `Also active on GNS3 (kept): ${live.other_filters.join(", ")}` : "";
  }
  const capturing = !!(live && live.capturing);
  startBtn.hidden = capturing;
  stopBtn.hidden = !capturing;
  wsBtn.hidden = false;
  startBtn.disabled = !live;
  stopBtn.disabled = !live;
  wsBtn.disabled = !capturing;
  startBtn.title = live ? "Start a packet capture on this link" : "No live GNS3 link found for this pair";
  stopBtn.title = "Stop the running capture";
  wsBtn.title = capturing ? "Open a live, in-browser packet view" : "Start a capture first";
  startBtn.onclick = () => callLinkCapture("start", node1, if1, node2, if2);
  stopBtn.onclick = () => callLinkCapture("stop", node1, if1, node2, if2);
  wsBtn.onclick = () => openCaptureViewer(node1, if1, node2, if2);
}

function showLinkActions(node1, if1, node2, if2, anchor) {
  selectedLinkPair = { node1, if1, node2, if2 };
  const popup = document.getElementById("link-actions");
  popup.hidden = false;
  renderLinkActionsPopup(node1, if1, node2, if2);
  positionNear(popup, anchor);
}

async function applyLinkOptions(values) {
  if (!selectedLinkPair) return;
  const { node1, if1, node2, if2 } = selectedLinkPair;
  try {
    const resp = await fetch(`/links/${encodeURIComponent(node1)}/${encodeURIComponent(node2)}/options${linkQuery(if1, if2)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(values),
    });
    if (!resp.ok) {
      await showAlert(`Couldn't set the link conditions: ${(await resp.json().catch(() => ({}))).detail || resp.status}`, { title: "Link conditions" });
      return;
    }
    await refreshLiveLinks();
    renderCanvas();
    if (selectedLinkPair) renderLinkActionsPopup(selectedLinkPair.node1, selectedLinkPair.if1, selectedLinkPair.node2, selectedLinkPair.if2);
    setMenubarMessage(`Link conditions updated on ${node1} <-> ${node2}.`);
  } catch (err) {
    await showAlert(`Couldn't set the link conditions: ${err}`, { title: "Link conditions" });
  }
}
document.getElementById("lo-apply").addEventListener("click", () => {
  const num = (id) => parseInt(document.getElementById(id).value, 10) || 0;
  applyLinkOptions({
    delay_ms: num("lo-delay"), jitter_ms: num("lo-jitter"), loss_percent: num("lo-loss"),
    corrupt_percent: num("lo-corrupt"), suspended: document.getElementById("lo-suspend").checked,
  });
});
document.getElementById("lo-reset").addEventListener("click", () => {
  applyLinkOptions({ delay_ms: 0, jitter_ms: 0, loss_percent: 0, corrupt_percent: 0, suspended: false });
});

function deselectLink() {
  selectedLinkPair = null;
  document.getElementById("link-actions").hidden = true;
}

async function callLinkCapture(action, node1, if1, node2, if2) {
  try {
    const resp = await fetch(
      `/links/${encodeURIComponent(node1)}/${encodeURIComponent(node2)}/capture/${action}${linkQuery(if1, if2)}`,
      { method: "POST" }
    );
    if (!resp.ok) {
      await showAlert(`Failed to ${action} capture: ${(await resp.json()).detail || resp.status}`);
      return;
    }
    await refreshLiveLinks();
    if (selectedLinkPair && linkKey(selectedLinkPair.node1, selectedLinkPair.if1, selectedLinkPair.node2, selectedLinkPair.if2) === linkKey(node1, if1, node2, if2)) {
      renderLinkActionsPopup(selectedLinkPair.node1, selectedLinkPair.if1, selectedLinkPair.node2, selectedLinkPair.if2);
    }
  } catch (err) {
    await showAlert(`Failed to ${action} capture: ${err}`);
  }
}

document.getElementById("link-delete-btn").addEventListener("click", async () => {
  if (!selectedLinkPair) return;
  const { node1, if1, node2, if2 } = selectedLinkPair;
  const deleteMsg = `Delete link ${node1}:${if1 || "?"} ⇄ ${node2}:${if2 || "?"}?`;
  if (!(await showConfirm(deleteMsg, { danger: true, confirmLabel: "Delete" }))) return;
  deselectLink();
  await deleteLink(node1, if1, node2, if2);
});

document.addEventListener("click", (evt) => {
  const popup = document.getElementById("link-actions");
  if (popup.hidden || popup.contains(evt.target)) return;
  if (evt.target.closest(".link-line, .link-hit")) return;
  deselectLink();
});

// --- Live in-browser packet view ---
//
// GET /links/{n1}/{n2}/capture/stream (app/main.py, proxying GNS3's own
// live pcap stream - see app/capture.py) hands back raw pcap bytes with
// no end, the same stream GNS3's own desktop client pipes into a local
// `wireshark -k -i -`. Rather than only offering that copyable command,
// PcapStreamParser decodes the stream client-side (pcap global header,
// then repeated packet records) into a Wireshark-style table, so a
// capture is viewable without leaving the browser or needing a local
// Wireshark install - the copyable command is still shown underneath
// for anyone who wants the real thing.
//
// Ethernet/ARP/IPv4/TCP/UDP/ICMP only (VLAN-tagged frames are unwrapped
// first) - enough for a useful summary line per packet, not a full
// protocol-dissector replacement for real Wireshark.

class PcapStreamParser {
  constructor(onPacket) {
    this.onPacket = onPacket;
    this.buf = new Uint8Array(0);
    this.haveGlobalHeader = false;
    this.littleEndian = true;
  }

  push(chunk) {
    const merged = new Uint8Array(this.buf.length + chunk.length);
    merged.set(this.buf, 0);
    merged.set(chunk, this.buf.length);
    this.buf = merged;
    this._drain();
  }

  _drain() {
    if (!this.haveGlobalHeader) {
      if (this.buf.length < 24) return;
      const dv = new DataView(this.buf.buffer, this.buf.byteOffset, 24);
      // 0xa1b2c3d4 (or the nanosecond-precision variant 0xa1b23c4d) read
      // little-endian means the file is little-endian; read big-endian
      // means it's big-endian - either way the magic number itself tells
      // us which to use for everything that follows.
      const leMagic = dv.getUint32(0, true);
      this.littleEndian = leMagic === 0xa1b2c3d4 || leMagic === 0xa1b23c4d;
      this.haveGlobalHeader = true;
      this.buf = this.buf.slice(24);
    }
    while (this.buf.length >= 16) {
      const dv = new DataView(this.buf.buffer, this.buf.byteOffset, 16);
      const tsSec = dv.getUint32(0, this.littleEndian);
      const tsUsec = dv.getUint32(4, this.littleEndian);
      const inclLen = dv.getUint32(8, this.littleEndian);
      if (this.buf.length < 16 + inclLen) break;
      const packetData = this.buf.slice(16, 16 + inclLen);
      this.buf = this.buf.slice(16 + inclLen);
      this.onPacket(tsSec, tsUsec, packetData);
    }
  }
}

function macStr(data, off) {
  return Array.from(data.slice(off, off + 6)).map((b) => b.toString(16).padStart(2, "0")).join(":");
}

function ipStr(data, off) {
  return `${data[off]}.${data[off + 1]}.${data[off + 2]}.${data[off + 3]}`;
}

function tcpFlagsStr(flags) {
  const names = [];
  if (flags & 0x02) names.push("SYN");
  if (flags & 0x10) names.push("ACK");
  if (flags & 0x01) names.push("FIN");
  if (flags & 0x04) names.push("RST");
  if (flags & 0x08) names.push("PSH");
  if (flags & 0x20) names.push("URG");
  return names.length ? `[${names.join(", ")}]` : "";
}

function protoName(n) {
  return { 1: "ICMP", 6: "TCP", 17: "UDP" }[n] || `proto ${n}`;
}

// Full dissection for one Ethernet frame - returns both the one-line
// `summary` for the packet-list row and a `layers` tree (title + [name,
// value] field pairs, outermost first) for the Wireshark-style detail
// pane a row expands into on click. Ethernet/VLAN/ARP/IPv4/TCP/UDP/ICMP
// only, same scope as the plain summary this replaces - IPv6 is
// identified but not decoded further.
function dissectPacket(data) {
  if (data.length < 14) {
    return {
      layers: [{ title: "Malformed frame", fields: [["Length", `${data.length} bytes`]] }],
      summary: { src: "", dst: "", proto: "?", info: "Malformed frame" },
    };
  }
  const dv = new DataView(data.buffer, data.byteOffset, data.length);
  const dstMac = macStr(data, 0);
  const srcMac = macStr(data, 6);
  let ethertype = dv.getUint16(12, false);
  let off = 14;
  const ethFields = [["Destination", dstMac], ["Source", srcMac]];
  if (ethertype === 0x8100 && data.length >= 18) {
    // 802.1Q VLAN tag - real ethertype is 4 bytes further in.
    ethFields.push(["802.1Q VLAN ID", `${dv.getUint16(14, false) & 0x0fff}`]);
    ethertype = dv.getUint16(16, false);
    off = 18;
  }
  ethFields.push(["Type", `0x${ethertype.toString(16).padStart(4, "0")}`]);
  const layers = [{ title: `Ethernet II, Src: ${srcMac}, Dst: ${dstMac}`, fields: ethFields }];
  const summary = { src: srcMac, dst: dstMac, proto: `0x${ethertype.toString(16)}`, info: "" };

  if (ethertype === 0x0800 && data.length >= off + 20) {
    const verIhl = data[off];
    const ihl = (verIhl & 0x0f) * 4;
    const totalLen = dv.getUint16(off + 2, false);
    const ttl = data[off + 8];
    const ipProto = data[off + 9];
    const srcIp = ipStr(data, off + 12);
    const dstIp = ipStr(data, off + 16);
    layers.push({
      title: `Internet Protocol Version 4, Src: ${srcIp}, Dst: ${dstIp}`,
      fields: [
        ["Version", `${verIhl >> 4}`],
        ["Header Length", `${ihl} bytes`],
        ["Total Length", `${totalLen}`],
        ["Time to Live", `${ttl}`],
        ["Protocol", `${protoName(ipProto)} (${ipProto})`],
        ["Source", srcIp],
        ["Destination", dstIp],
      ],
    });
    summary.src = srcIp;
    summary.dst = dstIp;
    const l4 = off + ihl;
    if (ipProto === 6 && data.length >= l4 + 20) {
      const sport = dv.getUint16(l4, false);
      const dport = dv.getUint16(l4 + 2, false);
      const seq = dv.getUint32(l4 + 4, false);
      const ack = dv.getUint32(l4 + 8, false);
      const dataOffset = (data[l4 + 12] >> 4) * 4;
      const flags = tcpFlagsStr(data[l4 + 13]);
      const window = dv.getUint16(l4 + 14, false);
      layers.push({
        title: `Transmission Control Protocol, Src Port: ${sport}, Dst Port: ${dport}`,
        fields: [
          ["Source Port", `${sport}`],
          ["Destination Port", `${dport}`],
          ["Sequence Number", `${seq}`],
          ["Acknowledgment Number", `${ack}`],
          ["Header Length", `${dataOffset} bytes`],
          ["Flags", flags || "-"],
          ["Window Size", `${window}`],
        ],
      });
      summary.proto = "TCP";
      summary.info = `${sport} → ${dport} ${flags}`.trim();
    } else if (ipProto === 17 && data.length >= l4 + 8) {
      const sport = dv.getUint16(l4, false);
      const dport = dv.getUint16(l4 + 2, false);
      const len = dv.getUint16(l4 + 4, false);
      layers.push({
        title: `User Datagram Protocol, Src Port: ${sport}, Dst Port: ${dport}`,
        fields: [["Source Port", `${sport}`], ["Destination Port", `${dport}`], ["Length", `${len}`]],
      });
      summary.proto = "UDP";
      summary.info = `${sport} → ${dport}`;
    } else if (ipProto === 1 && data.length >= l4 + 2) {
      layers.push({
        title: "Internet Control Message Protocol",
        fields: [["Type", `${data[l4]}`], ["Code", `${data[l4 + 1]}`]],
      });
      summary.proto = "ICMP";
      summary.info = `Type ${data[l4]}, Code ${data[l4 + 1]}`;
    } else {
      summary.proto = `IP proto ${ipProto}`;
    }
  } else if (ethertype === 0x0806 && data.length >= off + 28) {
    const opcode = dv.getUint16(off + 6, false);
    const senderMac = macStr(data, off + 8);
    const senderIp = ipStr(data, off + 14);
    const targetMac = macStr(data, off + 18);
    const targetIp = ipStr(data, off + 24);
    layers.push({
      title: `Address Resolution Protocol (${opcode === 1 ? "request" : "reply"})`,
      fields: [
        ["Opcode", opcode === 1 ? "request (1)" : "reply (2)"],
        ["Sender MAC", senderMac],
        ["Sender IP", senderIp],
        ["Target MAC", targetMac],
        ["Target IP", targetIp],
      ],
    });
    summary.src = senderMac;
    summary.dst = targetMac;
    summary.proto = "ARP";
    summary.info = opcode === 1 ? `Who has ${targetIp}? Tell ${senderIp}` : `${senderIp} is at ${senderMac}`;
  } else if (ethertype === 0x86dd) {
    layers.push({ title: "Internet Protocol Version 6", fields: [["Note", "Not decoded further"]] });
    summary.proto = "IPv6";
  }
  return { layers, summary };
}

function hexDump(data) {
  const lines = [];
  for (let i = 0; i < data.length; i += 16) {
    const chunk = data.slice(i, i + 16);
    const hex = Array.from(chunk).map((b) => b.toString(16).padStart(2, "0")).join(" ");
    const ascii = Array.from(chunk).map((b) => (b >= 32 && b < 127 ? String.fromCharCode(b) : ".")).join("");
    lines.push(`${i.toString(16).padStart(4, "0")}  ${hex.padEnd(47)}  ${ascii}`);
  }
  return lines.join("\n");
}

let captureViewerAbort = null;
let capturedPackets = [];   // parallel to the visible rows - trimmed together, see appendCaptureRow
let selectedPacketRow = null;

function appendCaptureRow(num, timeOffset, data, dissected) {
  const tbody = document.getElementById("capture-viewer-tbody");
  const row = document.createElement("tr");
  const { summary } = dissected;
  const cells = [num, timeOffset, summary.src, summary.dst, summary.proto, data.length, summary.info];
  cells.forEach((value, i) => {
    const td = document.createElement("td");
    td.textContent = value;
    if (i === 4) td.className = `capture-proto-${summary.proto}`;
    row.appendChild(td);
  });
  row.addEventListener("click", () => selectCaptureRow(row));
  tbody.appendChild(row);
  capturedPackets.push({ data, dissected });

  // Unbounded growth would eventually make the tab unresponsive on a
  // busy link left capturing for a while - keep only the most recent
  // 1000 rows, same spirit as the Logs panel's own tail-based approach.
  while (tbody.children.length > 1000) {
    tbody.removeChild(tbody.firstChild);
    capturedPackets.shift();
  }

  const wrap = document.getElementById("capture-viewer-table-wrap");
  const nearBottom = wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight < 60;
  if (nearBottom) wrap.scrollTop = wrap.scrollHeight;
}

function selectCaptureRow(row) {
  const tbody = document.getElementById("capture-viewer-tbody");
  const index = Array.prototype.indexOf.call(tbody.children, row);
  const packet = capturedPackets[index];
  if (!packet) return;

  document.querySelectorAll("#capture-viewer-tbody tr.selected").forEach((r) => r.classList.remove("selected"));
  row.classList.add("selected");
  selectedPacketRow = row;

  const tree = document.getElementById("capture-viewer-detail-tree");
  tree.innerHTML = "";
  for (const layer of packet.dissected.layers) {
    const details = document.createElement("details");
    details.open = true;
    const summaryEl = document.createElement("summary");
    summaryEl.textContent = layer.title;
    details.appendChild(summaryEl);
    for (const [name, value] of layer.fields) {
      const field = document.createElement("div");
      field.className = "detail-field";
      field.append(`${name}: `);
      const span = document.createElement("span");
      span.textContent = value;
      field.appendChild(span);
      details.appendChild(field);
    }
    tree.appendChild(details);
  }

  document.getElementById("capture-viewer-detail-hex").textContent = hexDump(packet.data);
  document.getElementById("capture-viewer-detail-wrap").hidden = false;
}

async function openCaptureViewer(node1, if1, node2, if2) {
  if (captureViewerAbort) captureViewerAbort.abort();
  const overlay = document.getElementById("capture-viewer-overlay");
  const status = document.getElementById("capture-viewer-status");
  const retryBtn = document.getElementById("capture-viewer-retry-btn");
  document.getElementById("capture-viewer-tbody").innerHTML = "";
  document.getElementById("capture-viewer-detail-wrap").hidden = true;
  capturedPackets = [];
  selectedPacketRow = null;
  document.getElementById("capture-viewer-title").textContent =
    `${node1}:${if1 || "?"} ⇄ ${node2}:${if2 || "?"} — live capture`;
  const streamPath = `/links/${encodeURIComponent(node1)}/${encodeURIComponent(node2)}/capture/stream${linkQuery(if1, if2)}`;
  const wiresharkCmd = `curl -sN '${window.location.origin}${streamPath}' | wireshark -k -i -`;
  document.getElementById("capture-viewer-wireshark-command").value = wiresharkCmd;
  document.getElementById("capture-viewer-wireshark-status").textContent = "";
  status.textContent = "Connecting…";
  retryBtn.hidden = true;
  retryBtn.onclick = () => openCaptureViewer(node1, if1, node2, if2);
  overlay.hidden = false;

  const controller = new AbortController();
  captureViewerAbort = controller;

  // GNS3's own /pcap endpoint is known to be intermittently flaky (see
  // app/capture.py's own docstring - the exact same request can work
  // once and then hang or drop on the very next attempt, a real
  // upstream gns3-server issue, not fixable from here) - offering an
  // immediate one-click retry instead of making the user close and
  // re-open the whole viewer is the practical mitigation.
  try {
    const resp = await fetch(streamPath, { signal: controller.signal });
    if (!resp.ok || !resp.body) {
      status.textContent = `Failed to open capture stream: ${resp.status}`;
      retryBtn.hidden = false;
      return;
    }
    status.textContent = "Live";
    const reader = resp.body.getReader();
    let packetNum = 0;
    let firstTs = null;
    const parser = new PcapStreamParser((tsSec, tsUsec, data) => {
      const ts = tsSec + tsUsec / 1e6;
      if (firstTs === null) firstTs = ts;
      packetNum += 1;
      appendCaptureRow(packetNum, (ts - firstTs).toFixed(6), data, dissectPacket(data));
    });
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      parser.push(value);
    }
    status.textContent = "Stream ended";
    retryBtn.hidden = false;
  } catch (err) {
    if (err.name !== "AbortError") {
      status.textContent = `Stream error: ${err}`;
      retryBtn.hidden = false;
    }
  }
}

document.getElementById("capture-viewer-copy-wireshark-btn").addEventListener("click", async () => {
  const input = document.getElementById("capture-viewer-wireshark-command");
  const status = document.getElementById("capture-viewer-wireshark-status");
  // The command is always visible and selectable in the input above
  // regardless of what happens here - clipboard write is best-effort
  // only, not the only way to get it. navigator.clipboard needs a
  // secure context (HTTPS or localhost); this app is commonly reached
  // over plain HTTP on a LAN IP, where it's simply unavailable - that
  // must degrade to an inline status message, never a popup (a popup
  // here previously could appear *after* the user had already moved on
  // and closed the capture viewer, since the clipboard rejection is
  // asynchronous - confusing, looked like a stray, unrelated dialog).
  try {
    await navigator.clipboard.writeText(input.value);
    status.textContent = "Copied!";
  } catch (err) {
    input.select();
    status.textContent = "Clipboard unavailable - selected for you, press Ctrl/Cmd+C";
  }
  setTimeout(() => { status.textContent = ""; }, 3000);
});

function closeCaptureViewer() {
  if (captureViewerAbort) {
    captureViewerAbort.abort();
    captureViewerAbort = null;
  }
  document.getElementById("capture-viewer-overlay").hidden = true;
}

document.getElementById("capture-viewer-close-btn").addEventListener("click", closeCaptureViewer);
document.getElementById("capture-viewer-clear-btn").addEventListener("click", () => {
  document.getElementById("capture-viewer-tbody").innerHTML = "";
  document.getElementById("capture-viewer-detail-wrap").hidden = true;
  capturedPackets = [];
  selectedPacketRow = null;
});

function exitLinkMode() {
  linkMode = false;
  linkFirstNode = null;
  linkFirstIf = null;
  document.getElementById("link-mode-btn").classList.remove("active");
  document.getElementById("link-mode-hint").hidden = true;
}

document.getElementById("link-mode-btn").addEventListener("click", () => {
  linkMode = !linkMode;
  linkFirstNode = null;
  linkFirstIf = null;
  document.getElementById("link-mode-btn").classList.toggle("active", linkMode);
  const hint = document.getElementById("link-mode-hint");
  hint.hidden = !linkMode;
  hint.textContent = "Click two nodes to connect them (Esc to cancel)";
});

window.addEventListener("keydown", (evt) => {
  if (evt.key !== "Escape") return;
  if (linkMode) exitLinkMode();
  if (placementImage) clearPlacementMode();
  if (drawMode) { drawDraft = null; renderDrawings(); setDrawMode(null); }
  if (closeOpenDrawingPopup) closeOpenDrawingPopup();
  closeAllMenus();
  if (!document.getElementById("node-actions").hidden || selectedNames.size) deselectNode();
});

// Left device palette (GNS3 Desktop's own node-selection dock, adapted
// to this web canvas): pick a category, click a device to arm
// placement, then click the canvas to drop it there - replaces the
// old dropdown-based Add Node form. classifyTemplateRole/
// buildRoleIconStandalone above are shared with the canvas's own node
// rendering, so the palette's icons/categorization always matches
// what a placed node actually looks like.
const PALETTE_CATEGORIES = [
  ["ALL", "All Devices"],
  ["ROUTER", "Routers"],
  ["SWITCH", "Switches"],
  ["PC", "End Devices"],
  ["FW", "Firewalls"],
  ["VM", "VMs"],
];
let paletteRoleFilter = "ALL";

async function ensureTemplatesLoaded() {
  if (availableTemplates) return availableTemplates;
  try {
    const resp = await fetch("/templates");
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    availableTemplates = await resp.json();
  } catch (err) {
    availableTemplates = { gns3: [], esxi: [], gns3_categories: {}, _error: String(err) };
  }
  return availableTemplates;
}

function renderPaletteList() {
  const list = document.getElementById("palette-list");
  list.innerHTML = "";
  if (!availableTemplates) {
    list.innerHTML = `<div class="palette-empty">Loading devices…</div>`;
    return;
  }
  if (availableTemplates._error) {
    list.innerHTML = `<div class="palette-empty">Failed to load templates: ${availableTemplates._error}</div>`;
    return;
  }
  const roleFilter = paletteRoleFilter === "ALL" ? null : paletteRoleFilter;
  const entries = [];
  for (const group of ["gns3", "esxi"]) {
    for (const name of availableTemplates[group]) {
      const role = classifyTemplateRole(name, group) || "PC";
      if (roleFilter && role !== roleFilter) continue;
      entries.push({ name, role });
    }
  }
  if (!entries.length) {
    list.innerHTML = `<div class="palette-empty">No devices in this category.</div>`;
    return;
  }
  for (const entry of entries) {
    const row = document.createElement("div");
    row.className = "palette-device";
    row.draggable = true;
    if (placementImage === entry.name) row.classList.add("armed");
    const iconWrap = document.createElement("span");
    iconWrap.className = "palette-icon";
    iconWrap.appendChild(buildRoleIconStandalone(isUplinkImage(entry.name) ? "UPLINK" : entry.role, 20));
    const label = document.createElement("span");
    label.textContent = isUplinkImage(entry.name) ? "NAT (Internet uplink)" : entry.name;
    row.appendChild(iconWrap);
    row.appendChild(label);
    row.addEventListener("click", () => armPlacement(entry.name, entry.role, row));
    // Same end result as click-to-arm-then-click-canvas, just via a
    // real HTML5 drag instead - GNS3 Desktop's own device tree works
    // the same way (drag a device onto the topology to place it).
    row.addEventListener("dragstart", (evt) => {
      evt.dataTransfer.effectAllowed = "copy";
      evt.dataTransfer.setData("application/json", JSON.stringify({ image: entry.name, role: entry.role }));
    });
    list.appendChild(row);
  }
}

function armPlacement(image, role, rowEl) {
  if (placementImage === image) {
    clearPlacementMode();
    return;
  }
  placementImage = image;
  placementRole = role;
  document.querySelectorAll(".palette-device.armed").forEach((el) => el.classList.remove("armed"));
  rowEl.classList.add("armed");
  document.getElementById("palette-hint-name").textContent = image;
  document.getElementById("palette-hint").hidden = false;
  document.getElementById("canvas").classList.add("placing");
}

function clearPlacementMode() {
  placementImage = null;
  placementRole = null;
  document.querySelectorAll(".palette-device.armed").forEach((el) => el.classList.remove("armed"));
  document.getElementById("palette-hint").hidden = true;
  document.getElementById("canvas").classList.remove("placing");
}

// Names of nodes whose "Add" is still deploying: not in nodesData yet, but taken.
const pendingNodeNames = new Set();
function nodeNameTaken(name) {
  const key = name.trim().toLowerCase();
  return Object.keys(nodesData).some((n) => n.toLowerCase() === key) ||
    [...pendingNodeNames].some((n) => n.toLowerCase() === key);
}

const ROLE_NAME_PREFIX = { ROUTER: "R", SWITCH: "SW", PC: "PC", FW: "FW", VM: "VM" };
function suggestNodeName(role, image) {
  if (isUplinkImage(image)) {
    if (!nodeNameTaken("Internet")) return "Internet";
    let n = 2;
    while (nodeNameTaken(`Internet${n}`)) n++;
    return `Internet${n}`;
  }
  const prefix = ROLE_NAME_PREFIX[role] || "Node";
  let i = 1;
  while (nodeNameTaken(`${prefix}${i}`)) i++;
  return `${prefix}${i}`;
}

async function placeNodeAt(point) {
  const role = placementRole;
  const image = placementImage;
  clearPlacementMode();
  if (snapToGridEnabled) {
    point = { x: snapToGrid(point.x), y: snapToGrid(point.y) };
  }
  const name = await showTextPrompt(`Name for the new ${image} node:`, {
    title: "Add Node",
    defaultValue: suggestNodeName(role, image),
  });
  if (!name) return;
  if (nodeNameTaken(name)) {
    await showAlert(
      `A node named "${name}" already exists or is just being added. Names must be unique (upper/lower case does not count).`,
      { title: "Add node" }
    );
    return;
  }
  if (!await confirmDisruption(
    `Add node "${name}" (${role}, ${image})?\n\nThis briefly disrupts every ` +
    `already-running ESXi-bridged Cloud connection, even for just this one node, ` +
    `and may take a while.`,
    { title: "Add node", confirmLabel: "Add" }
  )) {
    return;
  }
  setMenubarBusy(designMode ? `Adding "${name}"…` : `Adding "${name}" (deploying live, may take a while)…`);
  pendingNodeNames.add(name);
  try {
    const resp = await fetch(apiPath("/nodes"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, role, image }),
    });
    if (!resp.ok) {
      clearMenubarStatus();
      await showAlert(`Failed to add node: ${(await resp.json()).detail || resp.status}`);
      return;
    }
    await fetch(apiPath("/layout"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, x: point.x, y: point.y }),
    });
    await loadCanvas();
    fitViewToNodes();
    setMenubarMessage(`Added "${name}".`);
  } catch (err) {
    clearMenubarStatus();
    await showAlert(`Failed to add node: ${err}`);
  } finally {
    pendingNodeNames.delete(name);
  }
}

document.getElementById("palette-hint-cancel").addEventListener("click", clearPlacementMode);

document.querySelectorAll(".palette-cat").forEach((btn) => {
  btn.addEventListener("click", () => {
    paletteRoleFilter = btn.dataset.role;
    document.querySelectorAll(".palette-cat").forEach((b) => b.classList.toggle("active", b === btn));
    renderPaletteList();
  });
});

async function initPalette() {
  await ensureTemplatesLoaded();
  renderPaletteList();
}

function setDevicePaletteVisible(visible) {
  document.getElementById("device-palette").hidden = !visible;
  document.getElementById("toggle-devices-btn").classList.toggle("active", visible);
  if (!visible) clearPlacementMode();
}
document.getElementById("palette-close-btn").addEventListener("click", () => setDevicePaletteVisible(false));
document.getElementById("toggle-devices-btn").addEventListener("click", () => {
  setDevicePaletteVisible(document.getElementById("device-palette").hidden);
});

// Screenshot export: rasterizes the *whole diagram* (all nodes/links/
// drawings, auto-fit - not just whatever's currently panned/zoomed into
// view) as a PNG. Renders through a throwaway <img>/<canvas> rather than
// any library: a standalone serialized SVG carries none of the page's
// own external <style>, so a full copy of this page's one <style> block
// is embedded into the clone first - :root there still resolves the
// --accent/--canvas-bg/etc custom properties correctly, since :root in a
// parsed SVG document means the SVG's own root element.
const SCREENSHOT_PADDING = 40;
const SCREENSHOT_SCALE = 2;
const SCREENSHOT_MAX_DIMENSION = 8000;

function unionBBox(boxes) {
  const real = boxes.filter((b) => b && (b.width > 0 || b.height > 0));
  if (real.length === 0) return { x: 0, y: 0, width: 100, height: 100 };
  const minX = Math.min(...real.map((b) => b.x));
  const minY = Math.min(...real.map((b) => b.y));
  const maxX = Math.max(...real.map((b) => b.x + b.width));
  const maxY = Math.max(...real.map((b) => b.y + b.height));
  return { x: minX, y: minY, width: maxX - minX, height: maxY - minY };
}

async function takeCanvasScreenshot() {
  const layerIds = ["drawings-layer", "links-layer", "nodes-layer"];
  const layers = layerIds.map((id) => document.getElementById(id));
  let content;
  try {
    content = unionBBox(layers.map((el) => el.getBBox()));
  } catch {
    setMenubarMessage("Nothing to screenshot - canvas is empty.");
    return;
  }
  const vx = content.x - SCREENSHOT_PADDING;
  const vy = content.y - SCREENSHOT_PADDING;
  const vw = content.width + SCREENSHOT_PADDING * 2;
  const vh = content.height + SCREENSHOT_PADDING * 2;

  const exportSvg = document.createElementNS(SVG_NS, "svg");
  exportSvg.setAttribute("xmlns", SVG_NS);
  exportSvg.setAttribute("viewBox", `${vx} ${vy} ${vw} ${vh}`);
  const pixelScale = Math.min(SCREENSHOT_SCALE, SCREENSHOT_MAX_DIMENSION / Math.max(vw, vh, 1));
  exportSvg.setAttribute("width", Math.round(vw * pixelScale));
  exportSvg.setAttribute("height", Math.round(vh * pixelScale));

  const styleEl = document.createElementNS(SVG_NS, "style");
  styleEl.textContent = document.querySelector("style").textContent;
  exportSvg.appendChild(styleEl);

  const bg = document.createElementNS(SVG_NS, "rect");
  bg.setAttribute("x", vx);
  bg.setAttribute("y", vy);
  bg.setAttribute("width", vw);
  bg.setAttribute("height", vh);
  bg.setAttribute("fill", "var(--canvas-bg)");
  exportSvg.appendChild(bg);

  for (const layer of layers) {
    exportSvg.appendChild(layer.cloneNode(true));
  }
  exportSvg.querySelectorAll("image").forEach((img) => {
    const href = img.getAttribute("href") || img.getAttributeNS("http://www.w3.org/1999/xlink", "href");
    if (href && !href.startsWith("data:")) {
      const absolute = new URL(href, window.location.href).href;
      img.setAttribute("href", absolute);
      img.removeAttributeNS("http://www.w3.org/1999/xlink", "href");
    }
  });

  const svgText = new XMLSerializer().serializeToString(exportSvg);
  const svgBlob = new Blob([svgText], { type: "image/svg+xml;charset=utf-8" });
  const svgUrl = URL.createObjectURL(svgBlob);
  try {
    const image = new Image();
    await new Promise((resolve, reject) => {
      image.onload = resolve;
      image.onerror = () => reject(new Error("Failed to rasterize canvas SVG"));
      image.src = svgUrl;
    });
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(vw * pixelScale);
    canvas.height = Math.round(vh * pixelScale);
    const ctx = canvas.getContext("2d");
    ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
    const pngBlob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
    const pngUrl = URL.createObjectURL(pngBlob);
    const a = document.createElement("a");
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const projectMeta = document.getElementById("project-meta").textContent.split(" @ ")[0].trim();
    const safeName = (projectMeta || "topology").replace(/[^\w.-]+/g, "_");
    a.href = pngUrl;
    a.download = `${safeName}-${stamp}.png`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(pngUrl);
  } catch (err) {
    setMenubarMessage(`Screenshot failed: ${err.message}`);
  } finally {
    URL.revokeObjectURL(svgUrl);
  }
}
document.getElementById("take-screenshot-btn").addEventListener("click", () => { takeCanvasScreenshot(); });

// Loading feedback for slow, live-deploying actions - shown next to
// the menu bar's own buttons rather than a full-page overlay, since
// everything else (canvas, palette, topology list) stays usable while
// an import or add-node deploy is running in the background.
function setMenubarBusy(text) {
  const el = document.getElementById("menubar-status");
  el.classList.remove("menubar-error");
  el.innerHTML = "";
  const spinner = document.createElement("span");
  spinner.className = "menubar-spinner";
  const label = document.createElement("span");
  label.textContent = text;
  el.appendChild(spinner);
  el.appendChild(label);
}
// completed/total: see app/import_jobs.py's own docstring - this is a
// best-effort approximation from live-polling ESXi/GNS3 state, not a
// real callback from the deploy itself, so it can legitimately dip
// before climbing (a non-incremental deploy resets everything first).
function setMenubarProgress(text, completed, total) {
  const el = document.getElementById("menubar-status");
  el.classList.remove("menubar-error");
  el.innerHTML = "";
  const track = document.createElement("span");
  track.className = "menubar-progress-track";
  const fill = document.createElement("span");
  fill.className = "menubar-progress-fill";
  const pct = total > 0 ? Math.min(100, Math.round((completed / total) * 100)) : 0;
  fill.style.width = `${pct}%`;
  track.appendChild(fill);
  const label = document.createElement("span");
  label.textContent = total > 0 ? `${text} ${completed}/${total} (${pct}%)` : text;
  el.appendChild(track);
  el.appendChild(label);
}
// Wording that marks a message as a failure - shown in red. Pass
// {error: true/false} to override the guess for an odd phrasing.
const MENUBAR_ERROR_PATTERN = /\b(failed|failure|error|couldn't|can't|cannot|unavailable|refused|add a valid host|save this design)\b/i;
function setMenubarMessage(text, opts = {}) {
  const el = document.getElementById("menubar-status");
  el.textContent = text;
  el.classList.toggle("menubar-error", opts.error ?? MENUBAR_ERROR_PATTERN.test(text));
}
function clearMenubarStatus() {
  const el = document.getElementById("menubar-status");
  el.textContent = "";
  el.classList.remove("menubar-error");
}

const IMPORT_POLL_INTERVAL_MS = 1200;

// Polls GET /project/import/{jobId} (see app/import_jobs.py) until it
// leaves "running", updating the menubar progress bar on every tick.
// Resolves with the final job body either way ("done" or "error") -
// callers branch on .status rather than this throwing, since a failed
// import is an expected, handleable outcome, not a network exception.
async function pollImportJob(jobId) {
  while (true) {
    const body = await fetch(`/project/import/${jobId}`).then((r) => r.json());
    if (body.status !== "running") return body;
    setMenubarProgress("Importing project…", body.completed, body.total);
    await new Promise((resolve) => setTimeout(resolve, IMPORT_POLL_INTERVAL_MS));
  }
}

// Polls GET /gns3-vm/deploy/{jobId} (see app/gns3_vm_deploy_jobs.py)
// until it leaves "running". No completed/total here (see that
// module's own docstring on why) - just a coarse stage name shown as
// plain busy text instead of a progress bar.
async function pollGns3VmDeployJob(jobId) {
  while (true) {
    const body = await fetch(`/gns3-vm/deploy/${jobId}`).then((r) => r.json());
    if (body.status !== "running") return body;
    setMenubarBusy(`Deploying GNS3 VM: ${body.stage}…`);
    await new Promise((resolve) => setTimeout(resolve, IMPORT_POLL_INTERVAL_MS));
  }
}

document.getElementById("deploy-gns3-vm-btn").addEventListener("click", async () => {
  if (!await showConfirm(
    `Replace the GNS3 VM with a freshly imported one from its OVA?\n\n` +
    `If a VM by that name already exists, it's powered off and renamed ` +
    `as a timestamped backup before the new one is imported - this ` +
    `takes several minutes and the GNS3 VM (and every node on it) is ` +
    `unavailable throughout.`,
    { title: "Deploy Fresh GNS3 VM", confirmLabel: "Deploy", danger: true }
  )) {
    return;
  }

  // A second, separate confirmation - deleting the old VM is permanent
  // and irreversible, unlike the deploy itself (which always leaves the
  // old one as a recoverable backup on its own). Declining this still
  // proceeds with the deploy, just without deleting the backup - same
  // as before this option existed.
  const deleteOldVm = await showConfirm(
    `The deploy starts now. What should happen to the old VM once the new ` +
    `one is confirmed working?\n\n` +
    `"Keep as backup" leaves it powered off under a timestamped name. ` +
    `"Delete old VM" removes it permanently - that cannot be undone.`,
    { title: "Old VM", confirmLabel: "Delete old VM", cancelLabel: "Keep as backup", danger: true }
  );

  setMenubarBusy("Starting GNS3 VM deploy…");
  try {
    const resp = await fetch("/gns3-vm/deploy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ delete_old_vm: deleteOldVm }),
    });
    if (!resp.ok) {
      setMenubarMessage(`Deploy failed: ${(await resp.json()).detail || resp.status}`);
      return;
    }
    const { job_id } = await resp.json();
    const result = await pollGns3VmDeployJob(job_id);
    if (result.status === "error") {
      setMenubarMessage(`GNS3 VM deploy failed: ${result.error}`);
      return;
    }
    setMenubarMessage(
      `GNS3 VM deployed at ${result.ip_address}` +
      (deleteOldVm ? " (old VM deleted)." : ".")
    );
    await loadCanvas();
  } catch (err) {
    setMenubarMessage(`Deploy failed: ${err}`);
  }
});

document.getElementById("import-project-btn").addEventListener("click", () => {
  document.getElementById("import-project-input").click();
});

document.getElementById("import-project-input").addEventListener("change", async (evt) => {
  const input = evt.target;
  const file = input.files[0];
  input.value = ""; // allow re-selecting the same file next time
  if (!file) return;

  const importWarning = incrementalDeploy
    ? `This deploys the bundle's topology live, incrementally - it ` +
      `creates whatever's missing and reuses anything already running ` +
      `with a matching name, but never removes anything. This briefly ` +
      `disrupts every already-running ESXi-bridged Cloud connection, ` +
      `same as adding a node/link, and may take a while.`
    : `This deploys the bundle's topology live as a FULL, ` +
      `non-incremental deploy (the server's incremental_deploy setting ` +
      `is off) - it resets the ESXi vSwitch and redeploys every device ` +
      `from scratch, even ones that already exist by name. Anything ` +
      `currently running that isn't in this bundle may be deleted. ` +
      `This is more disruptive than a normal import and may take a ` +
      `while.`;
  // Whether this push happens automatically is the bundle's own choice
  // (manifest.json's auto_config_push, set when it was built), not
  // something visible here before upload - the note stays generic
  // rather than claiming either way.
  const autoPushNote = ` If this project file has auto_config_push ` +
    `enabled, its device configs also get pushed automatically right ` +
    `after the deploy succeeds - you'll see the results in the Config ` +
    `Push panel afterward either way.`;
  if (!await confirmDisruption(
    `Import "${file.name}"?\n\n${importWarning}${autoPushNote}`,
    { title: "Import project", confirmLabel: "Import" }
  )) {
    return;
  }

  setMenubarBusy("Starting import…");
  try {
    const formData = new FormData();
    formData.append("file", file);
    const resp = await fetch("/project/import", { method: "POST", body: formData });
    if (!resp.ok) {
      setMenubarMessage(`Import failed: ${(await resp.json()).detail || resp.status}`);
      return;
    }
    const { job_id } = await resp.json();
    const result = await pollImportJob(job_id);
    if (result.status === "error") {
      setMenubarMessage(`Import failed: ${result.error}`);
      return;
    }
    await loadCanvas();
    // Import already updated the server's own drawings state - use the
    // response directly rather than relying on loadCanvas's own
    // fetch-once guard, which would skip re-fetching here otherwise.
    if (Array.isArray(result.drawings)) {
      drawingsLoaded = true;
      drawings = result.drawings;
      drawIdCounter = Math.max(0, ...drawings.map((d) => d.id || 0));
      selectedDrawingId = null;
      renderDrawings();
    }

    if (Array.isArray(result.push_results)) {
      // The bundle's own manifest.json had auto_config_push enabled -
      // the server already ran config-push itself, so show what
      // happened instead of asking to load-then-manually-push.
      document.getElementById("push-input").value = JSON.stringify(result.configs, null, 2);
      renderPushResults(result.push_results);
      showTab("push");
      const failed = result.push_results.filter((r) => !r.ok).length;
      setMenubarMessage(
        `Imported "${result.manifest.project_name || file.name}" and pushed ` +
        `${result.push_results.length} device config(s)` +
        (failed ? ` (${failed} failed)` : "") + "."
      );
    } else {
      const configEntries = Object.entries(result.configs || {});
      if (configEntries.length && await showConfirm(
        `Load ${configEntries.length} device config(s) from this project into Config Push?`,
        { title: "Import project", confirmLabel: "Load" }
      )) {
        document.getElementById("push-input").value = JSON.stringify(result.configs, null, 2);
      }
      setMenubarMessage(`Imported "${result.manifest.project_name || file.name}".`);
    }
    fitViewToNodes();
  } catch (err) {
    setMenubarMessage(`Import failed: ${err}`);
  }
});

// Landing page - the app's own entry point, a grid of clickable host
// tiles, one per uploaded settings.yml profile (see
// app/settings_profiles.py). #app-view (the whole existing
// menubar+canvas UI) and #landing-view are mutually exclusive.
// Settings is a process-global loaded once at startup, so entering a
// host that isn't already active restarts the whole server against
// its settings file rather than swapping config in place - see that
// module's own docstring.
function showLandingView() {
  designMode = false;
  document.getElementById("app-view").hidden = true;
  document.getElementById("landing-view").hidden = false;
  loadLandingHostGrid();
}

// Toggles the handful of menu items that only make sense against a
// real, deployed host - nothing under /design/* has a live GNS3/ESXi
// connection to import into, reload, or push configs to.
function applyDesignModeUI() {
  document.getElementById("open-library-project-btn").hidden = !designMode;
  document.getElementById("save-library-project-btn").hidden = !designMode;
  document.getElementById("deploy-library-project-btn").hidden = !designMode;
  document.getElementById("design-project-separator").hidden = !designMode;
  document.getElementById("export-project-btn").href = designMode ? "/design/export" : "/project/export";
  document.getElementById("import-project-btn").hidden = designMode;
  document.getElementById("reload-gns3-btn").hidden = designMode;
  document.getElementById("deploy-gns3-vm-btn").hidden = designMode;
  document.getElementById("health-check-btn").hidden = designMode;
  document.getElementById("hold-project-btn").hidden = designMode;
  document.getElementById("find-orphans-btn").hidden = designMode;
  document.getElementById("tab-push").hidden = designMode;
  document.getElementById("nodes-menu").hidden = designMode;
  if (designMode) showTab("console");
}

async function enterDesignMode() {
  designMode = true;
  drawingsLoaded = false;
  initialFitDone = false;
  applyDesignModeUI();
  document.getElementById("landing-view").hidden = true;
  document.getElementById("app-view").hidden = false;
  try {
    if (typeof refreshCurrentDesignProject === "function") await refreshCurrentDesignProject();
    await loadCanvas();
  } catch (err) {
    setWarningBanner(`Failed to load design sketch: ${err}`);
  }
}

const GENERATE_POLL_INTERVAL_MS = 1500;

function setLandingGenerateStatus(text, warnings) {
  const el = document.getElementById("landing-generate-status");
  if (!text) { el.hidden = true; el.innerHTML = ""; return; }
  el.hidden = false;
  el.textContent = text;
  for (const warning of warnings || []) {
    const w = document.createElement("div");
    w.className = "landing-generate-warning";
    w.textContent = warning;
    el.appendChild(w);
  }
}

// POST /design/generate replaces whatever design sketch currently
// exists (see app/generate_jobs.py) - confirm first, same as any other
// destructive action, rather than silently discarding in-progress work.
async function promptAndGenerateTopology() {
  const existing = await fetch("/design/topology").then((r) => r.ok ? r.json() : []).catch(() => []);
  if (existing.length && !await showConfirm(
    `Generating a new topology replaces the current design sketch ` +
    `(${existing.length} node(s)). Continue?`,
    { title: "Generate via LLM", confirmLabel: "Replace", danger: true }
  )) {
    return;
  }

  const prompt = await showTextareaPrompt(
    "Describe the topology you want (e.g. \"a small office network with " +
    "2 routers, 3 switches, and 5 PCs\"):",
    { title: "Generate via LLM", placeholder: "Describe the topology…", confirmLabel: "Generate" }
  );
  if (!prompt) return;

  setLandingGenerateStatus("Starting generation…");
  try {
    const resp = await fetch("/design/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
    });
    if (!resp.ok) {
      setLandingGenerateStatus(null);
      await showAlert(`Generate failed: ${(await resp.json()).detail || resp.status}`, { title: "Generate via LLM" });
      return;
    }
    const { job_id } = await resp.json();

    let body;
    while (true) {
      body = await fetch(`/design/generate/${job_id}`).then((r) => r.json());
      if (body.status !== "running") break;
      setLandingGenerateStatus(
        `Generating (attempt ${body.attempt}/${body.max_attempts})…`,
        body.warnings
      );
      await new Promise((resolve) => setTimeout(resolve, GENERATE_POLL_INTERVAL_MS));
    }

    setLandingGenerateStatus(null);
    if (body.status === "error") {
      await showAlert(`Generate failed: ${body.error}`, { title: "Generate via LLM" });
      return;
    }
    await enterDesignMode();
  } catch (err) {
    setLandingGenerateStatus(null);
    await showAlert(`Generate failed: ${err}`, { title: "Generate via LLM" });
  }
}

async function loadLandingHostGrid() {
  const grid = document.getElementById("landing-host-grid");
  grid.textContent = "Loading…";
  let profiles;
  try {
    profiles = await fetch("/settings-profiles").then((r) => r.json());
  } catch (err) {
    grid.textContent = `Failed to load hosts: ${err}`;
    return;
  }

  grid.innerHTML = "";
  for (const profile of profiles) grid.appendChild(buildHostTile(profile));
  grid.appendChild(buildAddHostTile());

  const actionsGrid = document.getElementById("landing-actions-grid");
  actionsGrid.innerHTML = "";
  actionsGrid.appendChild(buildDesignTile());
  actionsGrid.appendChild(buildGenerateTile());
  const hostDesign = document.createElement("button");
  hostDesign.type = "button";
  hostDesign.className = "landing-host-tile add-tile";
  appendTileText(hostDesign, "Design a host configuration");
  hostDesign.addEventListener("click", () => openConnectionSettings());
  actionsGrid.appendChild(hostDesign);
}

function appendTileText(tile, title) {
  const name = document.createElement("div");
  name.className = "landing-host-name";
  name.textContent = title;
  tile.appendChild(name);
}

function buildDesignTile() {
  const tile = document.createElement("button");
  tile.type = "button";
  tile.className = "landing-host-tile add-tile";
  appendTileText(tile, "Design a Topology");
  tile.addEventListener("click", () => enterDesignMode());
  return tile;
}

function buildGenerateTile() {
  const tile = document.createElement("button");
  tile.type = "button";
  tile.className = "landing-host-tile add-tile";
  appendTileText(tile, "Generate via LLM");
  tile.addEventListener("click", () => promptAndGenerateTopology());
  return tile;
}

function buildHostTile(profile) {
  const tile = document.createElement("div");
  tile.className = "landing-host-tile";
  // Only a broken settings file gets a tooltip - its error text isn't
  // shown anywhere else on the tile.
  if (profile.error) tile.title = `${profile.filename}: ${profile.error}`;

  const deleteBtn = document.createElement("button");
  deleteBtn.className = "landing-host-delete-btn";
  deleteBtn.textContent = "Delete";
  deleteBtn.addEventListener("click", (evt) => {
    evt.stopPropagation();
    deleteHostProfile(profile.filename);
  });
  tile.appendChild(deleteBtn);
  const settingsBtn = document.createElement("button");
  settingsBtn.className = "toolbar-btn";
  settingsBtn.textContent = "Settings";
  settingsBtn.setAttribute("aria-label", `Settings for ${profile.filename}`);
  settingsBtn.addEventListener("click", (evt) => {
    evt.stopPropagation();
    openConnectionSettings(profile.filename);
  });
  tile.appendChild(settingsBtn);
  const projectBtn = document.createElement("button");
  projectBtn.className = "toolbar-btn";
  projectBtn.textContent = "Project";
  projectBtn.setAttribute("aria-label", `Project for ${profile.filename}`);
  projectBtn.addEventListener("click", evt => { evt.stopPropagation(); openProjectSelection(profile.filename); });
  tile.appendChild(projectBtn);


  const icon = document.createElement("img");
  icon.className = "landing-host-icon";
  icon.src = "/host-server-icon.svg";
  icon.alt = "";
  tile.appendChild(icon);

  // The ESXi IP (from inside the settings file) identifies a host far
  // better than its settings.yml filename does.
  const name = document.createElement("div");
  name.className = "landing-host-name";
  name.textContent = profile.error ? "invalid settings file" : (profile.esxi_ip || profile.filename);
  tile.appendChild(name);

  tile.tabIndex = 0;
  tile.setAttribute("aria-label", `Open host ${profile.esxi_ip || profile.filename}`);
  tile.addEventListener("keydown", event => {
    if (event.target === tile && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault(); enterHost(profile.filename, profile.active);
    }
  });
  tile.addEventListener("click", () => enterHost(profile.filename, profile.active));
  return tile;
}

function buildAddHostTile() {
  const tile = document.createElement("button");
  tile.type = "button";
  tile.className = "landing-host-tile add-tile";
  appendTileText(tile, "Add host");
  tile.addEventListener("click", () => document.getElementById("landing-upload-input").click());
  return tile;
}

let connectionFilename = null;
let connectionBusy = false;
let connectionLoaded = false;
let connectionGroups = [];
let connectionPreviewTimer = null;
let connectionDirty = false;
const connectionDialog = document.getElementById("connection-dialog");
const connectionForm = document.getElementById("connection-form");
const connectionResults = document.getElementById("connection-results");
const connectionApply = document.getElementById("connection-apply");
const connectionFields = document.getElementById("connection-fields");

async function settingsRequest(url, options = {}) {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${response.status})`);
  return body;
}
function connectionRequest(suffix = "", options = {}) {
  return settingsRequest(`/settings-profiles/${encodeURIComponent(connectionFilename)}/connections${suffix}`, options);
}
function settingsOptions(fields, extra = {}) {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ fields, ...extra }) };
}
// Entered passwords use field names "secret.<key>" and travel separately from
// the YAML fields - the server stores them apart from the profile file and
// never sends a value back (only whether one is stored).
const SECRET_PREFIX = "secret.";
const SECRET_FIELDS = [
  ["esxi_password", "ESXi password", "Password of the ESXi account above."],
  ["gns3_password", "GNS3 password", "Password of the GNS3 account in the GNS3 section."],
];
function settingsEntries() { return [...new FormData(connectionForm)]; }
function settingsValues() {
  return Object.fromEntries(settingsEntries().filter(([name]) => !name.startsWith(SECRET_PREFIX)));
}
function settingsSecrets() {
  return Object.fromEntries(settingsEntries()
    .filter(([name, value]) => name.startsWith(SECRET_PREFIX) && value)
    .map(([name, value]) => [name.slice(SECRET_PREFIX.length), value]));
}
function setPasswordHints(stored) {
  for (const [key] of SECRET_FIELDS) {
    const input = connectionForm.elements[SECRET_PREFIX + key];
    if (!input) continue;
    input.value = "";
    input.placeholder = stored?.[key] ? "A password is stored - leave empty to keep it" : "Not set - paste it here";
  }
}

async function openConnectionSettings(filename = null) {
  connectionFilename = filename;
  connectionLoaded = false;
  connectionDirty = false;
  connectionResults.textContent = "Loading settings…";
  connectionApply.hidden = true;
  document.getElementById("connection-yaml").textContent = "";
  document.getElementById("connection-title").textContent = filename ? `Settings — ${filename}` : "Design a host configuration";
  document.getElementById("connection-name-wrap").hidden = !!filename;
  document.getElementById("connection-name").value = "my-host.yml";
  connectionFields.replaceChildren();
  connectionDialog.showModal();
  setConnectionBusy(true);
  try {
    const [schema, body, ovaCatalog] = await Promise.all([
      settingsRequest("/host-config/schema"),
      filename ? connectionRequest() : Promise.resolve({ fields: {} }),
      settingsRequest("/ova-templates").catch(error => ({ templates: [], error: error.message })),
    ]);
    connectionGroups = schema.groups;
    for (const [index, group] of connectionGroups.entries()) {
      const section = document.createElement("fieldset");
      section.id = `connection-section-${index}`;
      const legend = document.createElement("legend");
      legend.textContent = group.title;
      section.appendChild(legend);
      for (const field of group.fields) {
        const label = document.createElement("label");
        const title = document.createElement("span");
        title.textContent = field.key.split(".").pop().replaceAll("_", " ").replace(/\b(esxi|gns3|api|vm|ova|mac|ip)\b/g, word => word.toUpperCase());
        label.appendChild(title);
        const input = document.createElement(field.type === "boolean" || field.type === "ova-template" ? "select" : field.type === "list" ? "textarea" : "input");
        if (field.type === "boolean") {
          for (const [value, text] of [["true", "Enabled"], ["false", "Disabled"]]) {
            const option = document.createElement("option"); option.value = value; option.textContent = text; input.appendChild(option);
          }
        } else if (field.type === "ova-template") {
          input.appendChild(new Option(ovaCatalog.templates.length ? "No GNS3 VM template selected" : "No GNS3 VM templates available", ""));
          for (const template of ovaCatalog.templates) {
            const details = [template.category, template.size].filter(Boolean).join(" · ");
            input.appendChild(new Option(details ? `${template.name} (${details})` : template.name, template.id));
          }
          const selected = body.fields[field.key] ?? field.default;
          if (selected && ![...input.options].some(option => option.value === selected)) {
            input.appendChild(new Option("Selected template is unavailable", selected));
          }
        } else if (field.type === "list") input.rows = 3;
        else {
          input.type = field.type;
          if (field.type === "number") { input.min = field.key.endsWith(".port") ? "1" : "0"; input.step = "1"; if (field.key.endsWith(".port")) input.max = "65535"; }
        }
        input.name = field.key;
        input.value = body.fields[field.key] ?? field.default;
        input.required = !field.optional;
        input.placeholder = field.optional ? "Automatic / not configured" : field.default;
        label.appendChild(input);
        const help = document.createElement("small");
        help.textContent = field.help || field.key;
        label.appendChild(help);
        section.appendChild(label);
      }
      connectionFields.appendChild(section);
    }
    connectionFields.appendChild(buildCredentialsSection());
    setPasswordHints(body.passwords_set);
    connectionLoaded = true;
    connectionResults.textContent = "";
    updateSettingsActions();
    await refreshSettingsPreview();
  } catch (error) { connectionResults.textContent = error.message; }
  finally { setConnectionBusy(false); }
}
function buildCredentialsSection() {
  const section = document.createElement("fieldset");
  section.id = "connection-section-credentials";
  const legend = document.createElement("legend");
  legend.textContent = "Credentials";
  section.appendChild(legend);
  for (const [key, title, help] of SECRET_FIELDS) {
    const label = document.createElement("label");
    const text = document.createElement("span");
    text.textContent = title;
    const input = document.createElement("input");
    input.type = "password";
    input.name = SECRET_PREFIX + key;
    input.autocomplete = "new-password";
    input.spellcheck = false;
    const note = document.createElement("small");
    note.textContent = `${help} Stored on the server only, never shown again.`;
    label.append(text, input, note);
    section.appendChild(label);
  }
  return section;
}
function updateSettingsActions() {
  document.getElementById("connection-save").textContent = connectionFilename ? "Save settings" : "Create host";
}
async function refreshSettingsPreview() {
  const target = document.getElementById("connection-yaml");
  try {
    target.textContent = (await settingsRequest("/host-config/preview", settingsOptions(settingsValues()))).yaml;
  } catch (error) {
    target.textContent = `The YAML can't be shown yet: ${error.message}`;
  }
}
function setConnectionBusy(busy) {
  connectionBusy = busy;
  connectionDialog.querySelectorAll("button, input, textarea, select").forEach(el => { el.disabled = busy || (!connectionLoaded && el.id !== "connection-close"); });
}
connectionForm.addEventListener("input", () => {
  clearTimeout(connectionPreviewTimer);
  connectionPreviewTimer = setTimeout(refreshSettingsPreview, 400);
  connectionDirty = true;
  connectionApply.hidden = true;
  connectionResults.textContent = "Unsaved changes. Connection results must be checked again after changes.";
});
connectionForm.addEventListener("submit", async event => {
  event.preventDefault();
  if (!connectionForm.reportValidity()) return;
  // Read before setConnectionBusy: disabled inputs are left out of FormData.
  const fields = settingsValues();
  const secrets = settingsSecrets();
  const filename = document.getElementById("connection-name").value.trim();
  if (!connectionFilename && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.ya?ml$/.test(filename)) {
    connectionResults.textContent = "Use a filename ending in .yml or .yaml, with letters, numbers, dots, hyphens or underscores."; return;
  }
  setConnectionBusy(true);
  try {
    if (connectionFilename) {
      await connectionRequest("", { ...settingsOptions(fields, { secrets }), method: "PUT" });
      connectionResults.textContent = "Saved. Apply / restart when ready. Other settings are retained; YAML comments are removed.";
      setPasswordHints((await connectionRequest()).passwords_set);
      connectionApply.hidden = false;
    } else {
      await settingsRequest("/host-config/create", settingsOptions(fields, { filename, secrets }));
      connectionFilename = filename;
      document.getElementById("connection-title").textContent = `Settings — ${filename}`;
      document.getElementById("connection-name-wrap").hidden = true;
      updateSettingsActions();
      setPasswordHints((await connectionRequest()).passwords_set);
      connectionResults.textContent = "Host created. It is available on the home screen. Apply / restart to activate it.";
      connectionApply.hidden = false;
    }
    connectionDirty = false;
    await loadLandingHostGrid();
  } catch (error) { connectionResults.textContent = error.message; }
  finally { setConnectionBusy(false); }
});
document.getElementById("connection-download").addEventListener("click", () => {
  const name = connectionFilename || document.getElementById("connection-name").value.trim() || "settings.yml";
  const url = URL.createObjectURL(new Blob([document.getElementById("connection-yaml").textContent], { type: "application/yaml" }));
  const link = document.createElement("a"); link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
document.getElementById("connection-test").addEventListener("click", async () => {
  if (!connectionForm.reportValidity()) return;
  const fields = settingsValues();
  setConnectionBusy(true);
  connectionResults.textContent = "Checking services from the server…";
  try {
    const options = settingsOptions(fields);
    const { results } = await (connectionFilename ? connectionRequest("/test", options) : settingsRequest("/host-config/test", options));
    connectionResults.replaceChildren();
    for (const result of results) {
      const card = document.createElement("section"); card.className = `connection-result ${result.status}`;
      const heading = document.createElement("strong"); heading.textContent = `${result.service} · ${result.status.toUpperCase()}`;
      const address = document.createElement("div"); address.textContent = result.address || "No discovered VM address";
      const detail = document.createElement("p"); detail.textContent = result.detail;
      const timing = document.createElement("small"); timing.textContent = result.duration_ms === undefined ? "Not tested" : `${result.check} · ${result.duration_ms} ms · ${new Date().toLocaleTimeString()}`;
      card.append(heading, address, detail, timing); connectionResults.appendChild(card);
    }
  } catch (error) { connectionResults.textContent = error.message; }
  finally { setConnectionBusy(false); }
});
function closeConnectionSettings() {
  if (connectionDirty && !window.confirm("Discard unsaved settings changes?")) return;
  connectionDialog.close();
}
document.getElementById("connection-close").addEventListener("click", closeConnectionSettings);
connectionDialog.addEventListener("cancel", event => { event.preventDefault(); if (!connectionBusy) closeConnectionSettings(); });
connectionApply.addEventListener("click", async () => { connectionDialog.close(); await enterHost(connectionFilename, false); });

async function enterHost(filename, isActive) {
  if (isActive) {
    designMode = false;
    liveHistory = { undo: [], redo: [] };
    lastPersistedDrawings = [];
    drawingsLoaded = false;
    initialFitDone = false;
    applyDesignModeUI();
    document.getElementById("landing-view").hidden = true;
    document.getElementById("app-view").hidden = false;
    try {
      await loadCanvas();
    } catch (err) {
      setWarningBanner(`Failed to load: ${err}`, { retryable: true });
    }
    return;
  }

  if (!await showConfirm(
    `Switch to "${filename}"? The server restarts against this settings file - any open ` +
    `console/screen connections drop for a moment.`,
    { title: "Switch host", confirmLabel: "Switch", danger: true }
  )) {
    return;
  }

  document.getElementById("landing-host-grid").textContent = `Restarting into "${filename}"…`;
  try {
    await fetch(`/settings-profiles/${encodeURIComponent(filename)}/activate`, { method: "POST" });
  } catch (err) {
    // Expected: the server process re-execs itself shortly after
    // responding (see restart_into_profile), which can drop this
    // request's own connection before a response body ever arrives -
    // that's not a real failure.
  }
  // Poll /health until the restarted process answers again, then just
  // reload the page outright - simplest way to get every piece of
  // state back in sync with the newly active host, rather than trying
  // to patch it all in place.
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    try {
      const resp = await fetch("/health");
      if (resp.ok) { window.location.reload(); return; }
    } catch (err) {
      // still restarting - keep polling
    }
  }
  await showAlert(`Server didn't come back after activating "${filename}" - check it manually.`, { title: "Switch host" });
  await loadLandingHostGrid();
}

async function deleteHostProfile(filename) {
  if (!await showConfirm(`Delete settings file "${filename}"? This doesn't affect a host it's currently active for.`, {
    title: "Delete host", confirmLabel: "Delete", danger: true,
  })) {
    return;
  }
  const resp = await fetch(`/settings-profiles/${encodeURIComponent(filename)}`, { method: "DELETE" });
  if (!resp.ok) {
    await showAlert(`Delete failed: ${(await resp.json()).detail || resp.status}`, { title: "Delete host" });
    return;
  }
  await loadLandingHostGrid();
}

document.getElementById("home-btn").addEventListener("click", () => showLandingView());
document.getElementById("landing-upload-input").addEventListener("change", async (evt) => {
  const input = evt.target;
  const file = input.files[0];
  input.value = "";
  if (!file) return;

  const formData = new FormData();
  formData.append("file", file);
  const resp = await fetch("/settings-profiles", { method: "POST", body: formData });
  if (!resp.ok) {
    await showAlert(`Upload failed: ${(await resp.json()).detail || resp.status}`, { title: "Add host" });
    return;
  }
  await loadLandingHostGrid();
});

document.getElementById("reload-gns3-btn").addEventListener("click", async () => {
  setMenubarBusy("Reloading GNS3 project…");
  try {
    const resp = await fetch("/project/reload-gns3", { method: "POST" });
    if (!resp.ok) {
      setMenubarMessage(`Reload failed: ${(await resp.json()).detail || resp.status}`);
      return;
    }
    await loadCanvas();
    setMenubarMessage("GNS3 project reloaded.");
  } catch (err) {
    setMenubarMessage(`Reload failed: ${err}`);
  }
});

// Compact text report of a /maintenance/verify result: every failed
// check, then the pass count - the full list of passing checks would
// only make the dialog overflow.
function formatVerifyReport(result) {
  const failed = result.results.filter((r) => !r.ok).map((r) => `[FAIL] ${r.description}`);
  const summary = `${result.passed}/${result.total} checks passed`;
  return failed.length ? `${failed.join("\n")}\n\n${summary}` : `All ${result.total} checks passed.`;
}

async function maintenanceRequest(url, options) {
  const resp = await fetch(url, options);
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.detail || resp.status);
  return body;
}

document.getElementById("health-check-btn").addEventListener("click", async () => {
  setMenubarBusy("Running health check…");
  try {
    const first = await maintenanceRequest("/maintenance/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fix: false }),
    });
    setMenubarMessage(`Health check: ${first.passed}/${first.total} checks passed.`, { error: first.passed !== first.total });
    if (first.passed === first.total) {
      await showAlert(formatVerifyReport(first), { title: "Health Check" });
      return;
    }
    const wantFix = await showConfirm(
      `${formatVerifyReport(first)}\n\n` +
      `Apply the automatic fixes (start stopped GNS3 nodes, power on stopped ` +
      `ESXi VMs, create missing port groups) and check again? Anything that ` +
      `needs a real redeploy is not touched.`,
      { title: "Health Check", confirmLabel: "Fix" }
    );
    if (!wantFix) return;

    setMenubarBusy("Applying fixes…");
    const second = await maintenanceRequest("/maintenance/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fix: true }),
    });
    const applied = second.actions.length
      ? `Applied:\n${second.actions.map((a) => `- ${a}`).join("\n")}\n\n`
      : "No automatic fix was applicable.\n\n";
    setMenubarMessage(`Health check after fixes: ${second.passed}/${second.total} checks passed.`, { error: second.passed !== second.total });
    await showAlert(applied + formatVerifyReport(second), { title: "Health Check" });
    await loadCanvas();

    // Never part of the automatic fixes: re-creating the Cloud nodes can briefly cut every
    // link between the GNS3 VM and the ESXi VMs, so it is its own, explicit confirmation.
    if (!second.results.some((r) => !r.ok && /VLAN subinterfaces/i.test(r.description))) return;
    const wantSubinterfaces = await showConfirm(
      "The GNS3 VM is missing VLAN subinterfaces (they are lost when it reboots), so links " +
      "between GNS3 nodes and ESXi VMs carry no traffic.\n\n" +
      "Re-create them? This restarts the Cloud nodes and briefly interrupts those links. " +
      "Do it while nothing important is running.",
      { title: "Re-create VLAN subinterfaces", confirmLabel: "Re-create", danger: true }
    );
    if (!wantSubinterfaces) return;

    setMenubarBusy("Re-creating VLAN subinterfaces…");
    const third = await maintenanceRequest("/maintenance/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fix: false, fix_vlan_subinterfaces: true }),
    });
    const repaired = third.actions.length
      ? `Applied:\n${third.actions.map((a) => `- ${a}`).join("\n")}\n\n`
      : "Nothing to re-create.\n\n";
    setMenubarMessage(`Health check after re-creating subinterfaces: ${third.passed}/${third.total} checks passed.`, { error: third.passed !== third.total });
    await showAlert(repaired + formatVerifyReport(third), { title: "Health Check" });
    await loadCanvas();
  } catch (err) {
    setMenubarMessage(`Health check failed: ${err.message || err}`);
  }
});

document.getElementById("find-orphans-btn").addEventListener("click", async () => {
  setMenubarBusy("Looking for orphans…");
  try {
    const { orphans } = await maintenanceRequest("/maintenance/orphans");
    if (!orphans.length) {
      setMenubarMessage("No orphans found.");
      await showAlert("No orphans found - every live node and tool-created VM is in the topology file.", { title: "Find Orphans" });
      return;
    }
    setMenubarMessage(`${orphans.length} orphan(s) found.`);
    const confirmed = await showConfirm(
      `${orphans.map((o) => `[${o.type}] ${o.name}`).join("\n")}\n\n` +
      `These exist live but no node in the topology file describes them. ` +
      `Permanently delete ${orphans.length === 1 ? "it" : "all of them"}? This cannot be undone.`,
      { title: `${orphans.length} Orphan(s) Found`, confirmLabel: "Delete", danger: true }
    );
    if (!confirmed) return;

    setMenubarBusy("Deleting orphans…");
    const { deleted } = await maintenanceRequest("/maintenance/orphans/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ orphans }),
    });
    setMenubarMessage(`Deleted ${deleted} orphan(s).`);
    await loadCanvas();
  } catch (err) {
    setMenubarMessage(`Find orphans failed: ${err.message || err}`);
  }
});

function openTerminalWebSocket(session, wsUrl) {
  const termHost = document.createElement("div");
  termHost.className = "session-terminal";
  session.bodyEl.innerHTML = "";
  session.bodyEl.appendChild(termHost);

  const term = new Terminal({ convertEol: true, fontSize: 13, theme: { background: "#1a1d1f" } });
  term.open(termHost);
  session.term = term;

  const ws = new WebSocket(wsUrl);
  ws.binaryType = "arraybuffer";
  session.ws = ws;

  ws.onopen = () => setSessionStatus(session, "connected", "status-ok");
  ws.onmessage = (event) => { term.write(new Uint8Array(event.data)); };
  ws.onclose = (event) => {
    setSessionStatus(session, event.reason ? `closed: ${event.reason}` : "closed", "status-bad");
  };
  ws.onerror = () => setSessionStatus(session, "error", "status-bad");

  term.onData((data) => {
    if (ws.readyState === WebSocket.OPEN) ws.send(new TextEncoder().encode(data));
  });
}

// The serial line of an ESXi VM, in the same terminal panel as a GNS3 console. Opening it again after it closed
// (VM was off, port busy) reconnects instead of just switching to the dead tab.
function openSerialConsole(nodeName) {
  const id = sessionIdFor(nodeName, "serial");
  const session = ensureSessionPanel(id, nodeName, `${nodeName} (serial)`);
  switchToSession(id);
  if (session.kind && session.ws && session.ws.readyState !== WebSocket.CLOSED) return;
  session.kind = "terminal";
  setSessionStatus(session, "connecting…");

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  openTerminalWebSocket(session, `${proto}//${location.host}/ws/serial/${encodeURIComponent(nodeName)}`);
  session.ws.onopen = () => setSessionStatus(session, "connected - press Enter if the screen stays empty", "status-ok");
}

function openConsole(nodeName) {
  const id = sessionIdFor(nodeName, "console");
  const session = ensureSessionPanel(id, nodeName, nodeName);
  switchToSession(id);
  if (session.kind) return; // already open or opening - just switched to its tab
  session.kind = "terminal";
  setSessionStatus(session, "connecting…");

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  openTerminalWebSocket(session, `${proto}//${location.host}/ws/console/${encodeURIComponent(nodeName)}`);
}

async function openVmScreen(nodeName, opts) {
  const isReconnect = !!(opts && opts.isReconnect);
  const id = sessionIdFor(nodeName, "screen");
  let session;
  if (isReconnect) {
    session = sessions.get(id);
    if (!session || !session.screenWanted) return; // tab closed/gave up while this was pending
  } else {
    session = ensureSessionPanel(id, nodeName, `${nodeName} (screen)`);
    switchToSession(id);
    if (session.kind) return;
    session.kind = "screen";
    session.screenWanted = true;
    session.reconnectAttempts = 0;
  }

  setSessionStatus(session, isReconnect ? "reconnecting…" : "connecting…");

  const screenHost = document.createElement("div");
  screenHost.className = "session-screen";
  session.bodyEl.innerHTML = "";
  session.bodyEl.appendChild(screenHost);

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/ws/vm-screen/${encodeURIComponent(nodeName)}`;

  let RFB;
  try {
    ({ default: RFB } = await import("/vendor/novnc/core/rfb.js"));
  } catch (err) {
    setSessionStatus(session, `couldn't load screen viewer: ${err}`, "status-bad");
    return;
  }
  if (!session.screenWanted) return; // closed while the module was loading

  const thisRfb = new RFB(screenHost, url, { wsProtocols: ["binary"] });
  session.rfb = thisRfb;
  thisRfb.scaleViewport = true;
  thisRfb.resizeSession = false;
  thisRfb.showDotCursor = true;

  thisRfb.addEventListener("connect", () => {
    session.reconnectAttempts = 0;
    setSessionStatus(session, "connected", "status-ok");
  });
  thisRfb.addEventListener("disconnect", () => {
    // session.rfb !== thisRfb: a newer attempt has already superseded
    // this one - don't double-reconnect or clobber its state.
    if (!session.screenWanted || session.rfb !== thisRfb) return;

    session.reconnectAttempts += 1;
    if (session.reconnectAttempts > VM_SCREEN_MAX_CONSECUTIVE_FAILURES) {
      setSessionStatus(session, "screen keeps disconnecting - giving up for now, click Screen to retry", "status-bad");
      session.screenWanted = false;
      return;
    }
    setSessionStatus(session, "reconnecting…");
    setTimeout(() => openVmScreen(nodeName, { isReconnect: true }), 300);
  });
  thisRfb.addEventListener("credentialsrequired", () => {
    setSessionStatus(session, "server requires credentials (not supported by this viewer)", "status-bad");
  });
  thisRfb.addEventListener("securityfailure", (evt) => {
    const detail = evt.detail || {};
    setSessionStatus(session, `security negotiation failed: ${detail.reason || detail.status || "unknown"}`, "status-bad");
  });
}

document.getElementById("tab-console").addEventListener("click", () => showTab("console"));
document.getElementById("tab-push").addEventListener("click", () => showTab("push"));
document.getElementById("tab-logs").addEventListener("click", () => showTab("logs"));

// GNS3 Desktop-style menu bar: click a menu-btn to toggle its dropdown,
// click anywhere else (including a menu-item, which bubbles here after
// running its own action) to close it - same "click outside closes it"
// convention as the rest of this app's own popovers.
function closeAllMenus() {
  document.querySelectorAll(".menu-dropdown").forEach((m) => { m.hidden = true; });
  document.querySelectorAll(".menu-btn").forEach((b) => b.classList.remove("open"));
}
document.querySelectorAll(".menu-btn[data-menu]").forEach((btn) => {
  btn.addEventListener("click", (evt) => {
    evt.stopPropagation();
    const menu = document.getElementById(`menu-${btn.dataset.menu}`);
    const wasOpen = !menu.hidden;
    closeAllMenus();
    if (!wasOpen) {
      menu.hidden = false;
      btn.classList.add("open");
    }
  });
});
document.addEventListener("click", closeAllMenus);

// The node-actions popup closes on a click anywhere else - except a
// click that itself just opened/repositioned it (a node icon or a
// Topology Summary row, both already handled by selectNode) or a
// click on one of its own buttons (Start/Stop/etc. stay open
// afterward, same as the old docked panel always did, so their
// result is visible without re-selecting the node).
function deselectNode() {
  selectedName = null;
  selectedNames = new Set();
  updateStatusBar();
  hideMultiActions();
  document.getElementById("node-actions").hidden = true;
  document.querySelectorAll(".node-shape.selected").forEach((g) => g.classList.remove("selected"));
  document.querySelectorAll(".summary-row.selected").forEach((r) => r.classList.remove("selected"));
}
let ignoreNextDocumentClick = false; // set when a rubber-band drag ends: its trailing click must not clear the new selection
document.addEventListener("click", (evt) => {
  if (ignoreNextDocumentClick) { ignoreNextDocumentClick = false; return; }
  const popup = document.getElementById("node-actions");
  const multi = document.getElementById("multi-actions");
  if (popup.hidden && multi.hidden) return;
  if (popup.contains(evt.target) || multi.contains(evt.target)) return;
  if (evt.target.closest(".node-shape") || evt.target.closest(".summary-row")) return;
  deselectNode();
});

// Settings has live fields (a number input, checkboxes) rather than
// one-shot commands - unlike File/View/Info's own menu-items, clicking
// inside it shouldn't immediately close it (the generic
// document-click-closes-everything listener above would otherwise
// close it after every single checkbox toggle).
document.getElementById("menu-settings").addEventListener("click", (evt) => evt.stopPropagation());

document.getElementById("settings-zoom-input").addEventListener("change", (evt) => {
  setZoomPercent(parseFloat(evt.target.value));
});

document.getElementById("settings-show-grid").addEventListener("change", (evt) => {
  document.querySelector(".grid-bg").style.display = evt.target.checked ? "" : "none";
});

document.getElementById("settings-snap-grid").addEventListener("change", (evt) => {
  snapToGridEnabled = evt.target.checked;
});

document.getElementById("reset-labels-btn").addEventListener("click", async () => {
  if (!Object.keys(labelOffsets).length) return;
  const changes = Object.fromEntries(Object.keys(labelOffsets).map((key) => [key, null]));
  labelOffsets = {};
  await saveLabelOffsets(changes);
  renderCanvas();
});

document.getElementById("settings-port-labels").addEventListener("change", (evt) => {
  showPortLabels = evt.target.checked;
  renderCanvas();
});

document.getElementById("settings-disruption-warning").addEventListener("change", (evt) => {
  showDisruptionWarnings = evt.target.checked;
});

document.querySelectorAll("#menu-draw .menu-item[data-draw]").forEach((btn) => {
  btn.addEventListener("click", () => setDrawMode(btn.dataset.draw));
});

// "Image" doesn't fit the other tools' click-drag-to-size flow (there's
// nothing to draw until a file is picked) - picking a file places it
// immediately, centered in the current view and already selected
// (resize handles showing) so it can be dragged/resized right away,
// same as a freshly-drawn rect/ellipse would be.
const DRAW_IMAGE_MAX_INITIAL_SIZE = 320;
document.getElementById("draw-image-btn").addEventListener("click", () => {
  setDrawMode(null);
  document.getElementById("draw-image-input").click();
});
document.getElementById("draw-image-input").addEventListener("change", async (evt) => {
  const input = evt.target;
  const file = input.files[0];
  input.value = "";
  if (!file) return;

  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  }).catch((err) => {
    showAlert(`Couldn't read "${file.name}": ${err}`, { title: "Add image" });
    return null;
  });
  if (!dataUrl) return;

  // Natural size for a sensible default placement - works for GIF/PNG/
  // JPEG directly; for SVG it depends on the file declaring its own
  // width/height or viewBox, and falls back to the same default box
  // rect/ellipse use (via naturalWidth/Height coming back 0) otherwise.
  const natural = await new Promise((resolve) => {
    const probe = new Image();
    probe.onload = () => resolve({ w: probe.naturalWidth, h: probe.naturalHeight });
    probe.onerror = () => resolve({ w: 0, h: 0 });
    probe.src = dataUrl;
  });

  let w = natural.w || 200;
  let h = natural.h || 150;
  if (w > DRAW_IMAGE_MAX_INITIAL_SIZE || h > DRAW_IMAGE_MAX_INITIAL_SIZE) {
    const scale = DRAW_IMAGE_MAX_INITIAL_SIZE / Math.max(w, h);
    w *= scale;
    h *= scale;
  }

  const x1 = viewBoxState.x + viewBoxState.w / 2 - w / 2;
  const y1 = viewBoxState.y + viewBoxState.h / 2 - h / 2;
  const d = { id: ++drawIdCounter, type: "image", x1, y1, x2: x1 + w, y2: y1 + h, href: dataUrl };
  drawings.push(d);
  selectedDrawingId = d.id;
  renderDrawings();
  persistDrawings();
});

document.getElementById("draw-clear-btn").addEventListener("click", async () => {
  if (!drawings.length) return;
  if (await showConfirm("Clear every drawing from the canvas?", { danger: true, confirmLabel: "Clear" })) {
    drawings = [];
    selectedDrawingId = null;
    renderDrawings();
    persistDrawings();
  }
});

function showTab(name) {
  document.getElementById("right-panel").hidden = name !== "console";
  document.getElementById("push-area").hidden = name !== "push";
  document.getElementById("logs-area").hidden = name !== "logs";
  document.getElementById("tab-console").classList.toggle("active", name === "console");
  document.getElementById("tab-push").classList.toggle("active", name === "push");
  document.getElementById("tab-logs").classList.toggle("active", name === "logs");
  if (name === "push" && !designMode) loadProjectConfigs(true);
  if (name === "logs") startLogsAutoRefresh(); else stopLogsAutoRefresh();
}

const LOGS_AUTO_REFRESH_INTERVAL_MS = 3000;
let logsAutoRefreshTimer = null;

async function loadLogs() {
  const output = document.getElementById("logs-output");
  let lines;
  try {
    lines = await fetch("/logs").then((r) => r.json());
  } catch (err) {
    output.innerHTML = "";
    const empty = document.createElement("div");
    empty.id = "logs-empty";
    empty.textContent = `Failed to load logs: ${err}`;
    output.appendChild(empty);
    return;
  }

  // Auto-scroll only if already at (or near) the bottom - otherwise a
  // periodic auto-refresh would keep yanking the view back down while
  // someone's trying to read an earlier entry.
  const wasAtBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 20;

  output.innerHTML = "";
  if (!lines.length) {
    const empty = document.createElement("div");
    empty.id = "logs-empty";
    empty.textContent = "No log entries yet.";
    output.appendChild(empty);
    return;
  }
  for (const entry of lines) {
    output.appendChild(buildLogLine(entry));
  }
  if (wasAtBottom) output.scrollTop = output.scrollHeight;
}

function buildLogLine(entry) {
  const row = document.createElement("div");
  row.className = "log-line";
  if (!entry.time) {
    // A line that didn't match the expected format (e.g. a traceback's
    // continuation lines) - shown verbatim, dimmed, no columns.
    row.classList.add("log-line-raw");
    row.textContent = entry.message;
    return row;
  }
  const meta = document.createElement("div");
  meta.className = "log-line-meta";
  const time = document.createElement("span");
  time.className = "log-line-time";
  time.textContent = entry.time;
  const level = document.createElement("span");
  level.className = `log-line-level log-level-${entry.level}`;
  level.textContent = entry.level;
  const location = document.createElement("span");
  location.className = "log-line-location";
  location.textContent = entry.location;
  meta.append(time, level, location);

  const message = document.createElement("div");
  message.className = `log-line-message log-level-${entry.level}`;
  message.textContent = entry.message;

  row.append(meta, message);
  return row;
}

function startLogsAutoRefresh() {
  loadLogs();
  stopLogsAutoRefresh();
  if (document.getElementById("logs-auto-refresh").checked) {
    logsAutoRefreshTimer = setInterval(loadLogs, LOGS_AUTO_REFRESH_INTERVAL_MS);
  }
}
function stopLogsAutoRefresh() {
  if (logsAutoRefreshTimer) {
    clearInterval(logsAutoRefreshTimer);
    logsAutoRefreshTimer = null;
  }
}
document.getElementById("logs-refresh-btn").addEventListener("click", () => loadLogs());
document.getElementById("logs-clear-btn").addEventListener("click", async () => {
  if (!await showConfirm(
    "Delete all log entries permanently? This can't be undone.",
    { title: "Clear logs", confirmLabel: "Clear", danger: true }
  )) return;
  const resp = await fetch("/logs", { method: "DELETE" });
  if (!resp.ok) {
    await showAlert(`Clearing the logs failed: ${(await resp.json().catch(() => ({}))).detail || resp.status}`, { title: "Clear logs" });
    return;
  }
  await loadLogs();
});
document.getElementById("logs-auto-refresh").addEventListener("change", () => startLogsAutoRefresh());

function renderPushResults(deviceResults) {
  const resultsEl = document.getElementById("push-results");
  resultsEl.replaceChildren();
  for (const result of deviceResults) {
    const box = document.createElement("div");
    box.className = "device-result";
    const header = document.createElement("div");
    header.className = `device-result-header ${result.ok ? "ok" : "fail"}`;
    const title = document.createElement("span");
    title.textContent = `${result.device} `;
    const tag = document.createElement("span");
    tag.className = "env-tag";
    tag.textContent = result.transport;
    title.appendChild(tag);
    const state = document.createElement("span");
    state.textContent = result.ok ? "ok" : "failed";
    header.append(title, state);
    box.appendChild(header);

    if (result.error) {
      const err = document.createElement("div");
      err.style.padding = "6px 10px";
      err.style.color = "var(--bad)";
      err.textContent = result.error;
      box.appendChild(err);
    }
    for (const c of result.commands || []) {
      const row = document.createElement("div");
      row.style.cssText = "display:flex;gap:8px;padding:3px 10px;align-items:baseline";
      const mark = document.createElement("span");
      mark.className = c.ok ? "cmd-ok" : "cmd-fail";
      mark.textContent = c.ok ? "✓" : "✗";
      const cmd = document.createElement("code");
      cmd.textContent = c.command;
      row.append(mark, cmd);
      box.appendChild(row);
      const output = (c.output || "").trim();
      if (output) {
        // Real command output (guest operations capture it too): shown in full,
        // folded open for a failure, always as text.
        const details = document.createElement("details");
        details.open = !c.ok;
        const summary = document.createElement("summary");
        summary.textContent = `output (${output.split("\n").length} line${output.includes("\n") ? "s" : ""})`;
        const pre = document.createElement("pre");
        pre.className = "cmd-output";
        pre.textContent = output;
        details.append(summary, pre);
        box.appendChild(details);
      }
    }
    resultsEl.appendChild(box);
  }
}

// --- Device configs saved with the project (library project of the active host) ---

function setPushNote(text, isError = false) {
  const note = document.getElementById("push-project-note");
  note.textContent = text;
  note.classList.toggle("push-note-error", isError);
}

async function loadProjectConfigs(silent = false) {
  try {
    const resp = await fetch("/project/configs");
    const body = await resp.json();
    if (!resp.ok) throw new Error(body.detail || resp.status);
    const count = Object.keys(body.devices).length;
    if (count) document.getElementById("push-input").value = JSON.stringify(body.devices, null, 2);
    document.getElementById("push-auto-checkbox").checked = !!body.auto_push;
    setPushNote(count
      ? `Loaded the configs of ${count} device${count === 1 ? "" : "s"} saved with this project.`
      : "This project has no saved configs yet - write them above and press Save to project.");
  } catch (err) {
    if (!silent) setPushNote(`Couldn't load: ${err.message || err}`, true);
  }
}

document.getElementById("push-load-project-btn").addEventListener("click", () => loadProjectConfigs());
document.getElementById("push-save-project-btn").addEventListener("click", async () => {
  let devices;
  try {
    devices = JSON.parse(document.getElementById("push-input").value);
  } catch (err) {
    setPushNote(`Invalid JSON: ${err}`, true);
    return;
  }
  try {
    const resp = await fetch("/project/configs", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ devices, auto_push: document.getElementById("push-auto-checkbox").checked }),
    });
    const body = await resp.json();
    if (!resp.ok) throw new Error(typeof body.detail === "string" ? body.detail : "invalid configs");
    const count = Object.keys(body.devices).length;
    setPushNote(`Saved with the project: ${count} device${count === 1 ? "" : "s"}${body.auto_push ? ", pushed automatically after deploy and resume" : ""}.`);
  } catch (err) {
    setPushNote(`Couldn't save: ${err.message || err}`, true);
  }
});

// Follows a config push job (POST /config-push/jobs) until it ends: per-device progress
// while it runs, the full per-device results afterwards.
async function followPushJob(jobId) {
  const resultsEl = document.getElementById("push-results");
  for (;;) {
    let body;
    try {
      const resp = await fetch(`/config-push/jobs/${jobId}`);
      body = await resp.json();
      if (!resp.ok) throw new Error(body.detail || resp.status);
    } catch (err) {
      resultsEl.textContent = `Lost the push job: ${err.message || err}`;
      return null;
    }
    if (body.status === "running") {
      renderPushProgress(body);
      await new Promise((resolve) => setTimeout(resolve, 1000));
      continue;
    }
    if (body.status === "error") {
      resultsEl.textContent = `The push failed: ${body.error}`;
      return null;
    }
    renderPushResults(body.results);
    return body.results;
  }
}

// The line shown while a push waits for devices that are still booting (job.waiting_for,
// retried by the server on its own); empty when nothing is waited for.
function pushWaitingText(names) {
  if (!Array.isArray(names) || names.length === 0) return "";
  return `Waiting for: ${names.join(", ")} (still booting)`;
}

function renderPushProgress(job) {
  const resultsEl = document.getElementById("push-results");
  resultsEl.replaceChildren();
  const heading = document.createElement("div");
  heading.style.cssText = "margin:6px 0;color:var(--dim);font-size:12px";
  heading.textContent = `Pushing… ${job.done_commands.toLocaleString()} of ${job.total_commands.toLocaleString()} commands, ${Math.round(job.elapsed_seconds)} s`;
  resultsEl.appendChild(heading);
  const waitingText = pushWaitingText(job.waiting_for);
  if (waitingText) {
    const waiting = document.createElement("div");
    waiting.style.cssText = "margin:6px 0;color:var(--dim);font-size:12px";
    waiting.textContent = waitingText;
    resultsEl.appendChild(waiting);
  }
  for (const device of job.devices) {
    const row = document.createElement("div");
    row.style.cssText = "display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12px";
    const name = document.createElement("span");
    name.style.minWidth = "80px";
    name.textContent = device.device;
    const track = document.createElement("span");
    track.className = "menubar-progress-track";
    track.style.flex = "1";
    const fill = document.createElement("span");
    fill.className = "menubar-progress-fill";
    fill.style.width = `${device.total ? Math.round((device.done / device.total) * 100) : 0}%`;
    track.appendChild(fill);
    const count = document.createElement("span");
    count.style.color = "var(--dim)";
    count.textContent = `${device.done.toLocaleString()} / ${device.total.toLocaleString()}`;
    row.append(name, track, count);
    resultsEl.appendChild(row);
  }
}

document.getElementById("push-btn").addEventListener("click", async () => {
  const resultsEl = document.getElementById("push-results");
  let devices;
  try {
    devices = JSON.parse(document.getElementById("push-input").value);
  } catch (err) {
    resultsEl.textContent = `Invalid JSON: ${err}`;
    return;
  }
  const button = document.getElementById("push-btn");
  button.disabled = true;
  resultsEl.textContent = "Starting…";
  try {
    const resp = await fetch("/config-push/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ devices }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      resultsEl.textContent = `Couldn't start the push: ${typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)}`;
      return;
    }
    await followPushJob(body.job_id);
  } catch (err) {
    resultsEl.textContent = `Request failed: ${err}`;
  } finally {
    button.disabled = false;
  }
});

// Drag #panel-resize-handle up/down to reallocate space between
// Topology Summary and the Console/Screen area below it.
(function setupPanelResize() {
  const handle = document.getElementById("panel-resize-handle");
  const topPanel = document.getElementById("topology-summary");
  let startY = 0;
  let startHeight = 0;

  function onMove(evt) {
    const dy = evt.clientY - startY;
    const min = 80;
    const max = document.getElementById("right-panel").clientHeight - 120;
    topPanel.style.height = `${Math.max(min, Math.min(startHeight + dy, max))}px`;
  }
  function onUp() {
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    handle.classList.remove("dragging");
  }
  handle.addEventListener("pointerdown", (evt) => {
    startY = evt.clientY;
    startHeight = topPanel.getBoundingClientRect().height;
    handle.classList.add("dragging");
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  });
})();

// Drag #right-panel-resize-handle left/right to resize the whole right
// panel (Topology Summary + Console/Screen together) - #push-area
// shares the same --right-panel-width custom property since it slots
// into the exact same spot when the View menu switches to it.
(function setupRightPanelResize() {
  const handle = document.getElementById("right-panel-resize-handle");
  const rightPanel = document.getElementById("right-panel");
  let startX = 0;
  let startWidth = 0;

  function onMove(evt) {
    const dx = startX - evt.clientX; // dragging left (toward canvas) widens the right panel
    const min = 220;
    const max = window.innerWidth - 420;
    const newWidth = Math.max(min, Math.min(startWidth + dx, max));
    document.documentElement.style.setProperty("--right-panel-width", `${newWidth}px`);
  }
  function onUp() {
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    handle.classList.remove("dragging");
  }
  handle.addEventListener("pointerdown", (evt) => {
    startX = evt.clientX;
    startWidth = rightPanel.getBoundingClientRect().width;
    handle.classList.add("dragging");
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  });
})();

// Pop the whole Console/Screen area out into its own real browser
// window - moves the actual live DOM subtree (not a copy), so every
// open xterm.js terminal/VNC screen and its WebSocket keeps running
// exactly as it was, uninterrupted. Moves it back into the right
// panel automatically if that window is closed.
let poppedOutWindow = null;
document.getElementById("popout-btn").addEventListener("click", (evt) => {
  if (poppedOutWindow && !poppedOutWindow.closed) {
    poppedOutWindow.focus();
    return;
  }
  const consoleArea = document.getElementById("console-area");
  const win = openPopOut(evt.shiftKey, "width=900,height=650");
  if (!win) {
    showAlert("Couldn't open a new window or tab - check your browser's popup blocker.");
    return;
  }
  win.document.head.innerHTML = document.head.innerHTML; // first: it also carries this page's own <title>
  win.document.title = "Ginnungagap - Console/Screen View";
  win.document.body.appendChild(consoleArea);
  poppedOutWindow = win;

  const placeholder = document.createElement("div");
  placeholder.id = "console-area-placeholder";
  placeholder.textContent = "Console/Screen View is popped out into its own window or tab.";
  document.getElementById("right-panel").appendChild(placeholder);

  win.addEventListener("beforeunload", () => {
    document.getElementById("console-area-placeholder")?.remove();
    document.getElementById("right-panel").appendChild(consoleArea);
    poppedOutWindow = null;
  });
});

showLandingView();
initPalette();

// The template service decides whether any topology validates at all; when
// it is down or lists nothing, every error reads "Image X not found" - so
// the real problem is shown on every screen instead of being guessed at.
const TEMPLATE_HEALTH_INTERVAL_MS = 60000;
async function refreshTemplateBanner() {
  const banner = document.getElementById("template-banner");
  try {
    const resp = await fetch("/templates/health");
    if (!resp.ok) return;
    const health = await resp.json();
    banner.textContent = health.ok ? "" : `Template service problem: ${health.problems.join(" ")}`;
    banner.hidden = health.ok;
  } catch (err) {
    // Backend not reachable right now - the other screens already say so.
  }
}
refreshTemplateBanner();
setInterval(refreshTemplateBanner, TEMPLATE_HEALTH_INTERVAL_MS);
