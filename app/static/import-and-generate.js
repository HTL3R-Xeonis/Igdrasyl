// [import-and-generate.js] Project import progress, the landing view with generate-via-LLM and the design tiles. Loaded in the order listed in index.html; all scripts share one global scope.
const IMPORT_POLL_INTERVAL_MS = 1200;

// "3:05 - creating WINSRV" / "3:05 - finishing (...)": the elapsed time and what the deploy is busy with, so a bar that
// stands still (a big Windows image takes minutes) does not look hung.
function importProgressDetail(body) {
  const seconds = body.elapsed_seconds || 0;
  const clock = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  const pending = body.pending || [];
  if (pending.length) {
    const shown = pending.slice(0, 3).join(", ") + (pending.length > 3 ? ` +${pending.length - 3}` : "");
    return `${clock}, still to create: ${shown}`;
  }
  return `${clock}, ${body.phase || "working"}`;
}

// Polls GET /project/import/{jobId} (see app/import_jobs.py) until it
// leaves "running", updating the menubar progress bar on every tick.
// Resolves with the final job body either way ("done" or "error") -
// callers branch on .status rather than this throwing, since a failed
// import is an expected, handleable outcome, not a network exception.
async function pollImportJob(jobId) {
  while (true) {
    const body = await fetch(`/project/import/${jobId}`).then((r) => r.json());
    if (body.status !== "running") return body;
    setMenubarProgress("Importing project…", body.completed, body.total, importProgressDetail(body));
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
  // On a host that deploys everything anew the import deletes what the bundle does not contain: name those devices,
  // and ask even when the disruption warnings are switched off - this one cannot be undone.
  let removed = [];
  try {
    const previewData = new FormData();
    previewData.append("file", file);
    const preview = await fetch("/project/import/preview", { method: "POST", body: previewData });
    if (preview.ok) removed = (await preview.json()).removed || [];
  } catch (err) {
    // No preview - the generic warning below still applies.
  }
  const importMessage = `Import "${file.name}"?\n\n${importWarning}${autoPushNote}`;
  const confirmed = removed.length
    ? await showConfirm(
      `Import "${file.name}"?\n\nThis host deploys everything anew (full deploy: the ESXi vSwitch is reset and every ` +
      `device is redeployed from scratch, which may take a while). These devices of the current project are not in ` +
      `the bundle and will be DELETED, together with their VMs: ${removed.join(", ")}.\n\n${autoPushNote.trim()}`,
      { title: "Import project - deletes devices", confirmLabel: "Delete and import", danger: true })
    : await confirmDisruption(importMessage, { title: "Import project", confirmLabel: "Import" });
  if (!confirmed) {
    return;
  }

  setMenubarBusy("Starting import…");
  lockCanvas("Import running - the canvas is locked until it is done");
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
  } finally {
    unlockCanvas();
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
  document.getElementById("menubar-mode-badge").hidden = !designMode;
  // Dims the topology summary + console area: a sketch has no live
  // status/console/screen, so the list only ever reads "no live data"
  // and the console placeholder's own advice ("open its Console or
  // Screen") never applies there - individual buttons are already
  // disabled per node (showNodeActions), this is just the visual cue
  // for the panel as a whole. Note/Delete stay fully usable, just a
  // little dimmed along with everything else.
  document.body.classList.toggle("design-mode", designMode);
  document.getElementById("open-library-project-btn").hidden = !designMode;
  document.getElementById("save-library-project-btn").hidden = !designMode;
  document.getElementById("deploy-library-project-btn").hidden = !designMode;
  document.getElementById("clear-design-btn").hidden = !designMode;
  document.getElementById("design-project-separator").hidden = !designMode;
  document.getElementById("export-project-btn").href = designMode ? "/design/export" : "/project/export";
  document.getElementById("import-project-btn").hidden = designMode;
  document.getElementById("reload-gns3-btn").hidden = designMode;
  document.getElementById("deploy-gns3-vm-btn").hidden = designMode;
  document.getElementById("health-check-btn").hidden = designMode;
  document.getElementById("connectivity-test-btn").hidden = designMode;
  document.getElementById("hold-project-btn").hidden = designMode;
  document.getElementById("find-orphans-btn").hidden = designMode;
  document.getElementById("tab-push").hidden = designMode;
  document.getElementById("nodes-menu").hidden = designMode;
  if (designMode) showTab("console");
}

document.getElementById("clear-design-btn").addEventListener("click", async () => {
  const ok = await showConfirm("Remove every device, link and drawing from this sketch? Edit > Undo brings it back.", {
    title: "Clear topology", danger: true, confirmLabel: "Clear",
  });
  if (!ok) return;
  const resp = await fetch("/design/reset", { method: "POST" });
  if (!resp.ok) {
    await showAlert(`Clearing the topology failed: ${(await resp.json().catch(() => ({}))).detail || resp.status}`, { title: "Clear topology" });
    return;
  }
  drawingsLoaded = false;
  deselectNode();
  if (typeof refreshCurrentDesignProject === "function") await refreshCurrentDesignProject();
  await loadCanvas();
  refreshHistoryState();
  setMenubarMessage("Topology cleared.");
});

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

  const prompt = await showTextareaPrompt("", {
    title: "Generate via LLM",
    placeholder: "Describe the topology you want (e.g. \"a small office network with 2 routers, 3 switches, and 5 PCs\")…",
    confirmLabel: "Generate",
  });
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
