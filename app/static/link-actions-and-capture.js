// [link-actions-and-capture.js] The link popup and the live packet view (capture). Loaded in the order listed in index.html; all scripts share one global scope.
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
  deselectDrawing();
});

// Left device palette (GNS3 Desktop's own node-selection dock, adapted
// to this web canvas): pick a category, click a device to arm
// placement, then click the canvas to drop it there - replaces the
// old dropdown-based Add Node form. classifyTemplateRole/
// buildRoleIconStandalone above are shared with the canvas's own node
// rendering, so the palette's icons/categorization always matches
// what a placed node actually looks like.
