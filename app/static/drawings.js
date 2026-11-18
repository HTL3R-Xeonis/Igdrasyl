// [drawings.js] Color parsing, the drawing popup, note marks and the status bar. Loaded in the order listed in index.html; all scripts share one global scope.
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
  // "Connected: 6 nodes (4 running)" - the running count belongs to the node count, the rest follows after a comma
  const parts = [`${nodes.length} node${nodes.length === 1 ? "" : "s"}${designMode ? "" : ` (${running} running)`}`];
  const links = Object.values(liveLinksData);
  const capturing = designMode ? 0 : links.filter((l) => l.capturing).length;
  const impaired = designMode ? 0 : links.filter((l) => l.suspended || l.delay_ms || l.loss_percent || l.corrupt_percent).length;
  if (capturing) parts.push(`${capturing} capturing`);
  if (impaired) parts.push(`${impaired} link${impaired === 1 ? "" : "s"} with conditions`);
  if (selectedNames.size > 1) parts.push(`${selectedNames.size} selected`);
  text.textContent = connectionOk
    ? `${designMode ? "Design" : "Connected"}: ${parts.join(", ")}`
    : "Server not reachable";
  const zoomPercent = Math.round((1600 / viewBoxState.w) * 100);
  document.getElementById("zoom-readout").textContent = `${zoomPercent}%`;
  const zoomInput = document.getElementById("settings-zoom-input");
  if (document.activeElement !== zoomInput) zoomInput.value = zoomPercent;
}
