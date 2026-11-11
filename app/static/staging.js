// [staging.js] Live-edit staging: stage several node/link changes
// against the live topology, review one diff, apply them all in one
// go - see app/staging.py/app/batch_apply.py. Deliberately a small,
// self-contained list-based dialog, not a re-skin of the main SVG
// canvas: the canvas's editing code is wired through apiPath() into
// many other design-mode-only features (layout, drawings, labels,
// notes, credentials) this does not support, and redirecting apiPath()
// itself for a third mode would risk breaking all of those instead of
// just adding a focused new one.
let stagingActive = false;

function setStagingUI(active) {
  stagingActive = active;
  document.getElementById("stage-changes-btn").hidden = designMode || active;
  document.getElementById("staging-indicator").hidden = !active;
}

document.getElementById("stage-changes-btn").addEventListener("click", async () => {
  const resp = await fetch("/live/stage", { method: "POST" });
  if (!resp.ok) {
    await showAlert(`Could not start staging: ${(await resp.json().catch(() => ({}))).detail || resp.status}`, { title: "Stage Changes" });
    return;
  }
  setStagingUI(true);
  document.getElementById("staging-count-text").textContent = "Staging changes…";
  await openStagingDialog();
});

document.getElementById("staging-count-text").addEventListener("click", openStagingDialog);

async function renderStagingLists() {
  const resp = await fetch("/live/stage/topology");
  if (!resp.ok) return;
  const nodes = await resp.json();

  const nodesList = document.getElementById("staging-nodes-list");
  nodesList.innerHTML = "";
  for (const node of nodes) {
    const li = document.createElement("li");
    li.textContent = `${node.name} (${node.role}, ${node.image}) `;
    const removeBtn = document.createElement("button");
    removeBtn.className = "toolbar-btn";
    removeBtn.type = "button";
    removeBtn.textContent = "Remove";
    removeBtn.addEventListener("click", () => removeStagedNode(node.name));
    li.appendChild(removeBtn);
    nodesList.appendChild(li);
  }

  const linksList = document.getElementById("staging-links-list");
  linksList.innerHTML = "";
  const seen = new Set();
  for (const node of nodes) {
    for (const link of node.links) {
      const key = [node.name, link.interface, link.peer, link.peer_interface].sort().join("|");
      if (seen.has(key)) continue;
      seen.add(key);
      const li = document.createElement("li");
      li.textContent = `${node.name}:${link.interface} ↔ ${link.peer}:${link.peer_interface} `;
      const removeBtn = document.createElement("button");
      removeBtn.className = "toolbar-btn";
      removeBtn.type = "button";
      removeBtn.textContent = "Remove";
      removeBtn.addEventListener("click", () => removeStagedLink(node.name, link.peer, link.interface, link.peer_interface));
      li.appendChild(removeBtn);
      linksList.appendChild(li);
    }
  }
}

async function openStagingDialog() {
  document.getElementById("staging-dialog-status").textContent = "";
  await renderStagingLists();
  document.getElementById("staging-dialog").showModal();
}

function stagingStatus(text) {
  document.getElementById("staging-dialog-status").textContent = text;
}

document.getElementById("staging-add-node-form").addEventListener("submit", async (evt) => {
  evt.preventDefault();
  const name = document.getElementById("staging-node-name").value.trim();
  const role = document.getElementById("staging-node-role").value.trim();
  const image = document.getElementById("staging-node-image").value.trim();
  if (!name || !role || !image) return;
  const resp = await fetch("/live/stage/nodes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, role, image }),
  });
  if (!resp.ok) {
    stagingStatus(`Could not add node: ${(await resp.json().catch(() => ({}))).detail || resp.status}`);
    return;
  }
  evt.target.reset();
  stagingStatus("");
  await renderStagingLists();
});

async function removeStagedNode(name) {
  const resp = await fetch(`/live/stage/nodes/${encodeURIComponent(name)}`, { method: "DELETE" });
  if (!resp.ok) {
    stagingStatus(`Could not remove node: ${(await resp.json().catch(() => ({}))).detail || resp.status}`);
    return;
  }
  await renderStagingLists();
}

document.getElementById("staging-add-link-form").addEventListener("submit", async (evt) => {
  evt.preventDefault();
  const node1 = document.getElementById("staging-link-node1").value.trim();
  const if1 = document.getElementById("staging-link-if1").value.trim();
  const node2 = document.getElementById("staging-link-node2").value.trim();
  const if2 = document.getElementById("staging-link-if2").value.trim();
  if (!node1 || !if1 || !node2 || !if2) return;
  const resp = await fetch("/live/stage/links", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ node1, if1, node2, if2 }),
  });
  if (!resp.ok) {
    stagingStatus(`Could not add link: ${(await resp.json().catch(() => ({}))).detail || resp.status}`);
    return;
  }
  evt.target.reset();
  stagingStatus("");
  await renderStagingLists();
});

async function removeStagedLink(node1, node2, if1, if2) {
  const query = `?if1=${encodeURIComponent(if1)}&if2=${encodeURIComponent(if2)}`;
  const resp = await fetch(`/live/stage/links/${encodeURIComponent(node1)}/${encodeURIComponent(node2)}${query}`, { method: "DELETE" });
  if (!resp.ok) {
    stagingStatus(`Could not remove link: ${(await resp.json().catch(() => ({}))).detail || resp.status}`);
    return;
  }
  await renderStagingLists();
}

function describeDiff(diff) {
  const lines = [];
  for (const node of diff.added_nodes) lines.push(`+ add node ${node.name} (${node.role}, ${node.image})`);
  for (const name of diff.removed_nodes) lines.push(`- remove node ${name}`);
  for (const [n1, i1, n2, i2] of diff.added_links) lines.push(`+ add link ${n1}:${i1} ↔ ${n2}:${i2}`);
  for (const [n1, i1, n2, i2] of diff.removed_links) lines.push(`- remove link ${n1}:${i1} ↔ ${n2}:${i2}`);
  return lines;
}

async function withStagingDialogSuspended(action) {
  const dialog = document.getElementById("staging-dialog");
  const wasOpen = dialog.open;
  if (wasOpen) dialog.close();
  try {
    return await action();
  } finally {
    if (wasOpen && stagingActive) dialog.showModal();
  }
}

async function endStagingSession() {
  setStagingUI(false);
  document.getElementById("staging-dialog").close();
  await loadCanvas();
}

document.getElementById("staging-dialog-review").addEventListener("click", async () => {
  const diffResp = await fetch("/live/stage/diff");
  if (!diffResp.ok) {
    const error = await diffResp.json().catch(() => ({}));
    stagingStatus(error.detail || `Could not compute the diff: ${diffResp.status}`);
    return;
  }
  const diff = await diffResp.json();
  const lines = describeDiff(diff);
  if (!lines.length) {
    stagingStatus("Nothing staged yet - add a node or link above first.");
    return;
  }
  const ok = await withStagingDialogSuspended(() => showConfirm(
    `This will be deployed live, one change at a time (each one briefly disrupts already-running ESXi-bridged connections):\n\n${lines.join("\n")}`,
    { title: "Review & Apply", confirmLabel: "Apply", danger: true }
  ));
  if (!ok) return;

  stagingStatus("Applying…");
  const resp = await fetch("/live/stage/apply", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: true, review_id: diff.review_id }),
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    stagingStatus(body.detail || `Could not apply changes: ${resp.status}`);
    return;
  }
  const summary = body.results
    .map((r) => `${r.ok ? "✓" : "✗"} ${r.action} ${r.target}${r.error ? ` - ${r.error}` : ""}`)
    .join("\n");
  if (body.applied) {
    await endStagingSession();
    await showAlert(summary || "Nothing to apply.", { title: "Applied" });
  } else {
    await withStagingDialogSuspended(() => showAlert(`Stopped at the first failure - already-applied steps stayed applied, staging is still open for the rest:\n\n${summary}`, { title: "Apply failed" }));
    await renderStagingLists();
  }
});

document.getElementById("staging-dialog-discard").addEventListener("click", async () => {
  const ok = await withStagingDialogSuspended(() => showConfirm("Discard every staged change? Nothing live is touched.", { title: "Discard staging", confirmLabel: "Discard", danger: true }));
  if (!ok) return;
  await fetch("/live/stage/discard", { method: "POST" });
  await endStagingSession();
});

document.getElementById("staging-discard-btn").addEventListener("click", () => document.getElementById("staging-dialog-discard").click());
document.getElementById("staging-apply-btn").addEventListener("click", openStagingDialog);
document.getElementById("staging-dialog-close").addEventListener("click", () => document.getElementById("staging-dialog").close());
