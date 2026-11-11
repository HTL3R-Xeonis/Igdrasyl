// [canvas-state.js] Canvas state and small helpers: node size, grid snapping, notes, the warning banner, link mode, role icons, edge geometry. Loaded in the order listed in index.html; all scripts share one global scope.
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
  document.getElementById("warning-banner-text").textContent = text && gns3VmProgress ? gns3VmProgress : text;
  banner.hidden = !text;
  retryBtn.hidden = !text || !opts.retryable;
  // Offered when the selected library project isn't deployed to GNS3 yet.
  warningDeployProject = text && opts.deployable ? opts.deployable : null;
  document.getElementById("warning-deploy-btn").hidden = !warningDeployProject;
  // Offered while the selected project is parked (see app/holds.py).
  warningHeldProject = opts.held ? opts.held : null;
  document.getElementById("warning-resume-btn").hidden = !warningHeldProject;
  // Offered while the GNS3 VM is switched off (Start) or on without an address (Restart), see app/gns3_vm_power.py.
  const gns3VmBtn = document.getElementById("warning-gns3vm-btn");
  warningGns3Vm = text && opts.gns3Vm && opts.gns3Vm.action ? opts.gns3Vm : null;
  gns3VmBtn.hidden = !warningGns3Vm;
  if (warningGns3Vm && !gns3VmBtn.disabled) {
    gns3VmBtn.textContent = gns3VmButtonLabel(warningGns3Vm.action);
  }
}
let warningGns3Vm = null;        // {name, power_state, action}
let templateGns3Vm = null;       // same, for the template-service banner
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
// Asks the template service to read the GNS3 servers again and waits (up to a minute) until it lists templates; the
// GNS3 service inside a freshly started VM can need a few more seconds than its address, so the request is repeated.
async function refreshTemplateService(requestFirst = true) {
  for (let attempt = 0; attempt < 10; attempt++) {
    if (attempt % 4 === 0 && (requestFirst || attempt > 0)) await fetch("/templates/refresh", { method: "POST" }).catch(() => {});
    await new Promise(resolve => setTimeout(resolve, 6000));
    const health = await fetch("/templates/health").then(r => r.json()).catch(() => null);
    if (health && health.ok) {
      // The startup warning of the app itself (e.g. "Image ... not found") was made while the list was empty: re-resolve it.
      await fetch("/reload", { method: "POST" }).catch(() => {});
      return;
    }
  }
}
// While a GNS3 VM start runs, both banners say what is going on instead of the (by then outdated) problem text.
let gns3VmProgress = null;
function setGns3VmProgress(text) {
  gns3VmProgress = text;
  for (const id of ["warning-banner-text", "template-banner-text"]) {
    const el = document.getElementById(id);
    if (text && el && el.textContent) el.textContent = text;
  }
}
function gns3VmButtonLabel(action) {
  return action === "restart" ? "Restart GNS3 VM" : action === "deploy" ? "Deploy GNS3 VM…" : "Start GNS3 VM";
}
// Starts (or, for a running VM without an address, restarts) the GNS3 VM; `btn` is the clicked banner button.
async function startGns3Vm(vm, btn) {
  if (!vm) return;
  // A VM that does not exist is deployed from its template: the File menu's own flow (confirmations, progress) does it.
  if (vm.action === "deploy") {
    document.getElementById("deploy-gns3-vm-btn").click();
    return;
  }
  if (vm.action === "restart" && !await showConfirm(
    `Restart the GNS3 VM "${vm.name}"? It is on but reports no address. A reset stops everything running inside it.`,
    { confirmLabel: "Restart", danger: true })) return;
  btn.disabled = true;
  btn.textContent = vm.action === "restart" ? "Restarting…" : "Starting…";
  setGns3VmProgress(
    `${vm.action === "restart" ? "Restarting" : "Starting"} the GNS3 VM "${vm.name}" - waiting for VMware Tools to report ` +
    "its address (a minute or two)…");
  let note = null;
  let failed = false;
  try {
    const resp = await fetch("/gns3-vm/start", { method: "POST" });
    if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || resp.statusText);
    // The VM needs a minute or two until VMware Tools report its address: ask the server to look again until it has one.
    const deadline = Date.now() + 5 * 60 * 1000;
    let resolved = false;
    while (Date.now() < deadline && !resolved) {
      await new Promise(resolve => setTimeout(resolve, 6000));
      const state = await (await fetch("/reload", { method: "POST" })).json();
      resolved = !!state.gns3_vm_ip;
    }
    if (!resolved) note = "The GNS3 VM was started but has no address yet - use Retry in a moment.";
    else {
      setGns3VmProgress("The GNS3 VM is up - waiting for the template service to list its templates…");
      await refreshTemplateService();
    }
  } catch (err) {
    note = `Could not start the GNS3 VM: ${err.message || err}`;
    failed = true;
  } finally {
    btn.disabled = false;
    gns3VmProgress = null;
  }
  if (!document.getElementById("app-view").hidden) await loadCanvas().catch(() => {});
  await refreshTemplateBanner();
  if (note) setMenubarMessage(note, { error: failed });
}
document.getElementById("template-refresh-btn").addEventListener("click", async (event) => {
  const btn = event.currentTarget;
  btn.disabled = true;
  btn.textContent = "Checking…";
  try {
    // Best effort: a service that cannot be asked (an older one) is simply checked again as it is - no message needed.
    const result = await fetch("/templates/refresh", { method: "POST" }).then(r => r.json()).catch(() => ({}));
    if (result.requested) await refreshTemplateService(false);
  } finally {
    btn.disabled = false;
    btn.textContent = "Check again";
    await refreshTemplateBanner();
  }
});
document.getElementById("warning-gns3vm-btn").addEventListener("click", (event) => startGns3Vm(warningGns3Vm, event.currentTarget));
document.getElementById("template-gns3vm-btn").addEventListener("click", (event) => startGns3Vm(templateGns3Vm, event.currentTarget));
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

// Node selection and drawing selection are two independent pieces of
// state (a node's own popup vs. a drawing's resize/rotate handles) -
// call this whenever a node gets selected so a previously selected
// drawing's handles don't linger on screen pointing at the wrong thing.
function deselectDrawing() {
  if (selectedDrawingId === null) return;
  selectedDrawingId = null;
  renderDrawings();
}
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
    setWarningBanner(health.warning || "", { retryable: !designMode, deployable: designMode ? null : health.deployable_project, held: designMode ? null : health.held_project, gns3Vm: designMode ? null : health.gns3_vm });
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
    if (health.esxi_ip) {
      esxiLink.href = `https://${health.esxi_ip}/`;
      esxiLink.classList.remove("menu-item-disabled");
    } else {
      esxiLink.href = "#";
      esxiLink.classList.add("menu-item-disabled");
    }

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
