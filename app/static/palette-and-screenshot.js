// [palette-and-screenshot.js] Device palette, node placement and naming, canvas lock, menubar messages, screenshot. Loaded in the order listed in index.html; all scripts share one global scope.
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

// Export the whole diagram (nodes, links and drawings) at its own bounds,
// independent of the current zoom and pan. Both formats share one styled SVG.
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

// All CSS of the page: inline <style> blocks plus the linked style sheets (the page's CSS lives in /styles.css, so
// there is no <style> element to copy any more).
async function pageCss() {
  const parts = [...document.querySelectorAll("style")].map((el) => el.textContent);
  for (const link of document.querySelectorAll('link[rel="stylesheet"]')) {
    if (new URL(link.href).origin !== window.location.origin) continue;
    try {
      const response = await fetch(link.href);
      if (response.ok) parts.push(await response.text());
    } catch {
      // a style sheet that cannot be read only costs the picture some styling
    }
  }
  return parts.join("\n");
}

async function inlineDiagramIcons(svg) {
  const iconUrls = new Set(Object.values(ROLE_ICON_FILES).map((path) => new URL(path, window.location.href).href));
  const dataUrls = new Map();
  await Promise.all(Array.from(svg.querySelectorAll("image"), async (img) => {
    const xlink = "http://www.w3.org/1999/xlink";
    const href = img.getAttribute("href") || img.getAttributeNS(xlink, "href");
    if (!href || href.startsWith("data:")) return;
    const url = new URL(href, window.location.href);
    if (!iconUrls.has(url.href)) {
      throw new Error("A linked image cannot be exported. Add it as an image file to include it.");
    }
    if (!dataUrls.has(url.href)) {
      dataUrls.set(url.href, (async () => {
        const response = await fetch(url.href, { mode: "same-origin" });
        if (!response.ok) throw new Error(`Could not load diagram image (${response.status}).`);
        const blob = await response.blob();
        return new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result);
          reader.onerror = () => reject(reader.error);
          reader.readAsDataURL(blob);
        });
      })());
    }
    const dataUrl = await dataUrls.get(url.href);
    img.removeAttributeNS(xlink, "href");
    img.setAttribute("href", dataUrl);
  }));
}

async function buildCanvasDiagram() {
  const layerIds = ["drawings-layer", "links-layer", "nodes-layer"];
  const layers = layerIds.map((id) => document.getElementById(id));
  let content;
  try {
    content = unionBBox(layers.map((el) => el.getBBox()));
  } catch {
    throw new Error("No diagram is visible.");
  }
  const vx = content.x - SCREENSHOT_PADDING;
  const vy = content.y - SCREENSHOT_PADDING;
  const vw = content.width + SCREENSHOT_PADDING * 2;
  const vh = content.height + SCREENSHOT_PADDING * 2;

  const exportSvg = document.createElementNS(SVG_NS, "svg");
  // XMLSerializer emits the SVG namespace from createElementNS.
  exportSvg.setAttribute("viewBox", `${vx} ${vy} ${vw} ${vh}`);
  const pixelScale = Math.min(SCREENSHOT_SCALE, SCREENSHOT_MAX_DIMENSION / Math.max(vw, vh, 1));
  const width = Math.round(vw * pixelScale);
  const height = Math.round(vh * pixelScale);
  exportSvg.setAttribute("width", width);
  exportSvg.setAttribute("height", height);

  const styleEl = document.createElementNS(SVG_NS, "style");
  styleEl.textContent = await pageCss();
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
  await inlineDiagramIcons(exportSvg);
  const svgText = new XMLSerializer().serializeToString(exportSvg);
  return { svgBlob: new Blob([svgText], { type: "image/svg+xml;charset=utf-8" }), width, height };
}

function downloadDiagram(blob, extension) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const projectMeta = document.getElementById("project-meta").textContent.split(" @ ")[0].trim();
  const safeName = (projectMeta || "topology").replace(/[^\w.-]+/g, "_");
  a.href = url;
  a.download = `${safeName}-${stamp}.${extension}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function takeCanvasScreenshot() {
  let svgUrl;
  try {
    const { svgBlob, width, height } = await buildCanvasDiagram();
    svgUrl = URL.createObjectURL(svgBlob);
    const image = new Image();
    await new Promise((resolve, reject) => {
      image.onload = resolve;
      image.onerror = () => reject(new Error("Failed to rasterize canvas SVG"));
      image.src = svgUrl;
    });
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
    const pngBlob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
    if (!pngBlob) throw new Error("The browser could not encode the PNG.");
    downloadDiagram(pngBlob, "png");
  } catch (err) {
    setMenubarMessage(`Screenshot failed: ${err.message}`);
  } finally {
    if (svgUrl) URL.revokeObjectURL(svgUrl);
  }
}

async function exportCanvasSvg() {
  try {
    const { svgBlob } = await buildCanvasDiagram();
    downloadDiagram(svgBlob, "svg");
  } catch (err) {
    setMenubarMessage(`SVG export failed: ${err.message}`);
  }
}

document.getElementById("take-screenshot-btn").addEventListener("click", () => { takeCanvasScreenshot(); });
document.getElementById("export-svg-btn").addEventListener("click", () => { exportCanvasSvg(); });

// Loading feedback for slow, live-deploying actions - shown next to
// the menu bar's own buttons rather than a full-page overlay, since
// everything else (canvas, palette, topology list) stays usable while
// an import or add-node deploy is running in the background.
// Locks the canvas (and the palette / device list beside it) while something replaces the whole project: an import
// deletes and redeploys devices, so clicking, dragging or deleting on the old picture would act on things that are
// going away. unlockCanvas() runs in a finally - a failed import must never leave the page locked.
let canvasLocked = false;
function lockCanvas(text) {
  canvasLocked = true;
  if (linkMode) exitLinkMode();
  if (placementImage) clearPlacementMode();
  if (drawMode) { drawDraft = null; renderDrawings(); setDrawMode(null); }
  deselectNode();
  document.getElementById("canvas-lock-text").textContent = text;
  document.getElementById("canvas-lock").hidden = false;
  document.body.classList.add("canvas-locked");
}
function unlockCanvas() {
  canvasLocked = false;
  document.getElementById("canvas-lock").hidden = true;
  document.body.classList.remove("canvas-locked");
}

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
function setMenubarProgress(text, completed, total, detail = "") {
  const el = document.getElementById("menubar-status");
  el.classList.remove("menubar-error");
  el.innerHTML = "";
  const track = document.createElement("span");
  track.className = "menubar-progress-track";
  const fill = document.createElement("span");
  fill.className = "menubar-progress-fill";
  // Never 100 % while the job still runs: the last devices are created, the rest (links, VLANs, checks) is not done.
  const pct = total > 0 ? Math.min(detail ? 99 : 100, Math.round((completed / total) * 100)) : 0;
  fill.style.width = `${pct}%`;
  track.appendChild(fill);
  const label = document.createElement("span");
  label.textContent = total > 0 ? `${text} ${completed}/${total} (${pct}%)${detail ? " - " + detail : ""}` : text;
  el.appendChild(track);
  el.appendChild(label);
}
// Wording that marks a message as a failure - shown in red. Pass
// {error: true/false} to override the guess for an odd phrasing.
const MENUBAR_ERROR_PATTERN = /\b(failed|failure|error|couldn't|can't|cannot|unavailable|refused|add a valid host|save this design)\b/i;
function setMenubarMessage(text, opts = {}) {
  const el = document.getElementById("menubar-status");
  el.replaceChildren();
  if (text) {
    const label = document.createElement("span");
    label.textContent = text;
    // A small x clears the message - nothing else does, until the next one replaces it.
    const close = document.createElement("button");
    close.type = "button";
    close.className = "menubar-dismiss";
    close.textContent = "\u00d7";
    close.setAttribute("aria-label", "Dismiss message");
    close.addEventListener("click", clearMenubarStatus);
    el.appendChild(label);
    if (opts.action) {
      const action = document.createElement("button");
      action.type = "button";
      action.className = "toolbar-btn";
      action.textContent = opts.action.label;
      action.addEventListener("click", async () => {
        if (action.disabled) return;
        action.disabled = true;
        try {
          await opts.action.onClick();
          action.remove();
        } catch (error) {
          setMenubarMessage(error.message, { error: true });
        } finally {
          action.disabled = false;
        }
      });
      el.appendChild(action);
    }
    el.appendChild(close);
  }
  el.classList.toggle("menubar-error", opts.error ?? MENUBAR_ERROR_PATTERN.test(text));
}
function clearMenubarStatus() {
  const el = document.getElementById("menubar-status");
  el.replaceChildren();
  el.classList.remove("menubar-error");
}
