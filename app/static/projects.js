// Project library and host selection. Loaded after shared app helpers.
let projectSelectionHost = null;
let currentDesignProject = null;
let projectLibraryBusy = false;
let projectSelectionLoaded = false;
const projectSelectionDialog = document.getElementById("project-selection-dialog");
const projectSelectionStatus = document.getElementById("project-selection-status");
const projectSelectionForm = document.getElementById("project-selection-form");
const projectPicker = document.getElementById("project-library-picker");
const projectApply = document.getElementById("project-selection-apply");
const projectSave = document.getElementById("project-selection-save");

function projectBusy(busy) {
  projectLibraryBusy = busy;
  projectSelectionDialog.querySelectorAll("input, select, button").forEach(el => { el.disabled = busy; });
  projectSave.disabled = busy || !projectSelectionLoaded;
}
function updateProjectDownload() {
  const link = document.getElementById("project-library-download");
  const id = projectPicker.value;
  link.hidden = !id || id === "external";
  if (!link.hidden) link.href = `/projects/${encodeURIComponent(id)}/download`;
  document.getElementById("project-library-open").hidden = !id || id === "external";
  const deleteBtn = document.getElementById("project-library-delete");
  deleteBtn.hidden = !id || id === "external";
  const held = projectPicker.selectedOptions[0]?.dataset.held === "1";
  deleteBtn.disabled = held;
  deleteBtn.title = held ? "On hold - resume it first" : "";
}

async function refreshCurrentDesignProject() {
  const body = await settingsRequest("/design/project");
  currentDesignProject = body.project;
  const label = currentDesignProject ? currentDesignProject.name : "Unsaved design";
  if (designMode) document.getElementById("project-meta").textContent = label;
  document.getElementById("save-library-project-btn").textContent = currentDesignProject
    ? `Save “${currentDesignProject.name}”`
    : "Save Project…";
  return currentDesignProject;
}
async function refreshProjectLibrary(selected = "") {
  const body = await settingsRequest("/projects");
  projectPicker.replaceChildren(new Option("No project selected", ""));
  const counts = new Map();
  body.projects.forEach(project => counts.set(project.name, (counts.get(project.name) || 0) + 1));
  body.projects.forEach(project => {
    const base = counts.get(project.name) > 1 ? `${project.name} (${project.id.slice(0, 8)})` : project.name;
    const option = new Option(project.held ? `${base} — on hold` : base, project.id);
    option.dataset.held = project.held ? "1" : "";
    projectPicker.add(option);
  });
  projectPicker.value = selected;
  projectSelectionForm.hidden = body.projects.length === 0;
  updateProjectDownload();
  return body.projects.length;
}
async function openProjectSelection(filename = null) {
  projectSelectionHost = filename;
  projectSelectionLoaded = false;
  projectSelectionStatus.textContent = "Loading…";
  projectSelectionForm.hidden = true;
  projectApply.hidden = true;
  projectSave.hidden = !filename;
  document.getElementById("project-name-label").hidden = !filename;
  document.getElementById("project-selection-help").hidden = !filename;
  document.getElementById("project-selection-name").value = "";
  document.getElementById("project-library-create").reset();
  projectSelectionDialog.showModal();
  projectBusy(true);
  try {
    const selection = filename ? await settingsRequest(`/settings-profiles/${encodeURIComponent(filename)}/project`) : {};
    const projectCount = await refreshProjectLibrary(selection.project_id || "");
    document.getElementById("project-selection-name").value = selection.project_name || "";
    projectSelectionLoaded = true;
    projectSelectionStatus.textContent = projectCount
      ? "Projects are stored independently of host settings."
      : "No projects yet. Create an empty project or upload a topology YAML below.";
  } catch (error) { projectSelectionStatus.textContent = error.message; }
  finally { projectBusy(false); }
}
document.getElementById("open-project-library").addEventListener("click", () => openProjectSelection());
document.getElementById("open-library-project-btn").addEventListener("click", () => openProjectSelection());
projectPicker.addEventListener("change", () => {
  updateProjectDownload();
  document.getElementById("project-selection-name").value = "";
});
document.getElementById("project-selection-form").addEventListener("input", () => { projectApply.hidden = true; });
document.getElementById("project-library-create").addEventListener("submit", async event => {
  event.preventDefault();
  if (projectLibraryBusy) return;
  projectBusy(true);
  projectApply.hidden = true;
  try {
    const file = document.getElementById("project-library-file").files[0];
    if (file && file.size > 2 * 1024 * 1024) throw new Error("Topology YAML must be no larger than 2 MB");
    const payload = { name: document.getElementById("project-library-name").value };
    if (file) payload.topology_yaml = await file.text();
    const project = await settingsRequest("/projects", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload) });
    await refreshProjectLibrary(project.id);
    document.getElementById("project-selection-name").value = "";
    document.getElementById("project-library-create").reset();
    projectSelectionStatus.textContent = `Added “${project.name}”.${projectSelectionHost ? " Save selection to use it with this host." : " You can select it from any host’s Project button."}`;
  } catch (error) { projectSelectionStatus.textContent = error.message; }
  finally { projectBusy(false); }
});
document.getElementById("project-selection-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (projectLibraryBusy || !projectSelectionLoaded || !projectSelectionHost) return;
  const id = projectPicker.value;
  const payload = { project_id: id || null,
    project_name: document.getElementById("project-selection-name").value };
  projectBusy(true);
  try {
    await settingsRequest(`/settings-profiles/${encodeURIComponent(projectSelectionHost)}/project`, { method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload) });
    projectSelectionStatus.textContent = "Selection saved. Apply / restart to use it. No deployment was started.";
    projectApply.hidden = false;
  } catch (error) { projectSelectionStatus.textContent = error.message; }
  finally { projectBusy(false); }
});
projectSelectionDialog.addEventListener("cancel", event => { if (projectLibraryBusy) event.preventDefault(); });
document.getElementById("project-selection-close").addEventListener("click", () => projectSelectionDialog.close());
projectApply.addEventListener("click", async () => { projectSelectionDialog.close(); await enterHost(projectSelectionHost, false); });

document.getElementById("project-library-open").addEventListener("click", async () => {
  const id = projectPicker.value;
  if (!id || id === "external" || projectLibraryBusy) return;
  projectBusy(true);
  try {
    const body = await settingsRequest(`/design/project/${encodeURIComponent(id)}/open`, { method:"POST" });
    currentDesignProject = body.project;
    projectSelectionDialog.close();
    await enterDesignMode();
    setMenubarMessage(`Opened “${currentDesignProject.name}”.`);
  } catch (error) { projectSelectionStatus.textContent = error.message; }
  finally { projectBusy(false); }
});

// Saves the current design to the project library: into its project when it
// has one, otherwise under a name asked for right here - no detour through
// the project dialog.
async function saveDesignProject(name) {
  const body = currentDesignProject && (!name || name === currentDesignProject.name)
    ? await settingsRequest(`/design/project/${encodeURIComponent(currentDesignProject.id)}`, { method: "PUT" })
    : await settingsRequest("/design/project", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
    });
  currentDesignProject = body.project;
  await refreshCurrentDesignProject();
  return currentDesignProject;
}

document.getElementById("save-library-project-btn").addEventListener("click", async () => {
  let name = null;
  if (!currentDesignProject) {
    name = await showTextPrompt("Name for this design:", { title: "Save project", defaultValue: "" });
    if (!name || !name.trim()) return;
    name = name.trim();
  }
  setMenubarBusy(`Saving “${name || currentDesignProject.name}”…`);
  try {
    const project = await saveDesignProject(name);
    setMenubarMessage(`Saved “${project.name}” to the project library.`);
  } catch (error) { setMenubarMessage(`Save failed: ${error.message}`); }
});

const deploymentDialog = document.getElementById("deployment-preview-dialog");
const deploymentHostPicker = document.getElementById("deployment-host-picker");
const deploymentContent = document.getElementById("deployment-preview-content");
const deploymentStatus = document.getElementById("deployment-preview-status");
const deploymentConfirm = document.getElementById("deployment-confirm");
const deploymentName = document.getElementById("deployment-project-name");
const deploymentNameHint = document.getElementById("deployment-name-hint");
let deploymentPreview = null;

function renderDeploymentPreview(preview) {
  deploymentPreview = preview;
  const roles = Object.entries(preview.roles).map(([role, count]) => `${role}: ${count}`).join(", ") || "None";
  const rows = [
    ["Project", preview.project.name],
    ["Host", preview.host.esxi_ip || preview.host.filename],
    ["GNS3 VM", preview.host.gns3_vm_name || "Not configured"],
    ["Topology", `${preview.node_count} nodes, ${preview.edge_count} links`],
    ["Roles", roles],
    ["Deployment mode", preview.mode === "incremental" ? "Incremental — reuse matching devices" : "Full — rebuild the topology"],
  ];
  const dl = document.createElement("dl"); dl.className = "deployment-summary";
  for (const [term, value] of rows) {
    const row = document.createElement("div"); row.className = "deployment-summary-row";
    const dt = document.createElement("dt"); dt.textContent = term;
    const dd = document.createElement("dd"); dd.textContent = value;
    row.append(dt, dd); dl.appendChild(row);
  }
  deploymentContent.replaceChildren(dl);
  const activation = preview.requires_restart
    ? "This project will be selected for the host. The app will restart, then begin deployment."
    : "The host and project are active. Deployment can start immediately.";
  const impact = preview.mode === "full"
    ? " Full deployment rebuilds every device and may remove managed devices absent from this project."
    : " Incremental deployment reuses matching devices and does not remove devices absent from this project.";
  deploymentStatus.textContent = preview.held
    ? "This project is on hold: its VMs and GNS3 project are kept. It is resumed, not deployed again."
      + (preview.requires_restart ? " The app first selects it for the host and restarts." : "")
    : activation + impact;
  deploymentStatus.className = preview.mode === "full" ? "deployment-warning" : "";
  deploymentConfirm.textContent = preview.held
    ? (preview.requires_restart ? "Prepare and resume" : "Resume now")
    : (preview.requires_restart ? "Prepare and deploy" : "Deploy now");
  deploymentConfirm.disabled = false;
}

// What the dialog is about to do with the name: deploy the saved project, save
// the edits to it first, or save the design as a new project.
function deploymentNameMode() {
  const typed = deploymentName.value.trim();
  if (!currentDesignProject) return "new";
  return typed && typed !== currentDesignProject.name ? "new" : "existing";
}

function renderUnsavedDeployment() {
  deploymentPreview = null;
  const nodes = Object.keys(nodesData).length;
  deploymentContent.replaceChildren();
  deploymentNameHint.textContent = deploymentNameMode() === "new"
    ? "The design is saved to the project library under this name, then deployed."
    : `Your latest edits are saved to “${currentDesignProject.name}”, then it is deployed.`;
  deploymentStatus.textContent = `${nodes} node${nodes === 1 ? "" : "s"} in this design. You can review the deployment mode after choosing Save and deploy.`;
  deploymentStatus.className = "";
  deploymentConfirm.textContent = "Save and deploy";
  deploymentConfirm.disabled = false;
}

let deploymentPreviewRequest = 0; // only the newest request may draw: typing a name fires one per keystroke
async function loadDeploymentPreview() {
  const request = ++deploymentPreviewRequest;
  // Unsaved, or about to be saved under a new name: nothing to preview yet.
  if (deploymentNameMode() === "new") { renderUnsavedDeployment(); return; }
  deploymentNameHint.textContent = `Your latest edits are saved to “${currentDesignProject.name}” first.`;
  deploymentConfirm.disabled = true;
  deploymentStatus.textContent = "Loading preview…";
  try {
    const preview = await settingsRequest(`/projects/${encodeURIComponent(currentDesignProject.id)}/deploy-preview/${encodeURIComponent(deploymentHostPicker.value)}`);
    if (request === deploymentPreviewRequest) renderDeploymentPreview(preview);
  } catch (error) {
    if (request === deploymentPreviewRequest) deploymentStatus.textContent = error.message;
  }
}

async function openDeploymentPreview() {
  await refreshCurrentDesignProject();
  deploymentName.value = currentDesignProject ? currentDesignProject.name : "";
  const profiles = await settingsRequest("/settings-profiles");
  deploymentHostPicker.replaceChildren();
  profiles.filter(profile => !profile.error).forEach(profile => {
    const label = `${profile.esxi_ip || profile.filename}${profile.active ? " (active)" : ""}`;
    deploymentHostPicker.add(new Option(label, profile.filename));
  });
  if (!deploymentHostPicker.options.length) {
    setMenubarMessage("Add a valid host before deploying."); return;
  }
  deploymentDialog.showModal();
  await loadDeploymentPreview();
}

async function monitorLibraryDeployment(project, jobId) {
  setMenubarBusy(`Deploying “${project.name}”…`);
  const result = await pollImportJob(jobId);
  if (result.status === "error") {
    setMenubarMessage(`Deployment failed: ${result.error}`); return;
  }
  await loadCanvas();
  setMenubarMessage(`Deployed “${project.name}” successfully.${describePush(result.push_results)}`, { error: pushHadFailures(result.push_results) });
  showPushResults(result.push_results);
}

// The project's saved device configs are pushed automatically after a deploy
// or resume when its "push automatically" switch is on (server side).
function pushHadFailures(results) {
  return Array.isArray(results) && results.some((r) => !r.ok);
}
function describePush(results) {
  if (!Array.isArray(results)) return "";
  const failed = results.filter((r) => !r.ok).length;
  return ` Configs pushed to ${results.length} device${results.length === 1 ? "" : "s"}${failed ? `, ${failed} failed (see Config Push)` : ""}.`;
}
function showPushResults(results) {
  if (Array.isArray(results)) renderPushResults(results);
}

async function startLibraryDeployment(project, filename) {
  const body = await settingsRequest(`/projects/${encodeURIComponent(project.id)}/deploy/${encodeURIComponent(filename)}`, { method:"POST" });
  deploymentDialog.close();
  await monitorLibraryDeployment(project, body.job_id);
}

document.getElementById("deploy-library-project-btn").addEventListener("click", openDeploymentPreview);
deploymentHostPicker.addEventListener("change", loadDeploymentPreview);
deploymentName.addEventListener("input", loadDeploymentPreview);
document.getElementById("deployment-preview-close").addEventListener("click", () => deploymentDialog.close());
deploymentConfirm.addEventListener("click", async () => {
  if (deploymentConfirm.disabled) return;
  const name = deploymentName.value.trim();
  if (!currentDesignProject && !name) {
    deploymentStatus.textContent = "Enter a name for this design first.";
    deploymentName.focus();
    return;
  }
  deploymentConfirm.disabled = true;
  try {
    // Always deploy what is on the canvas now: save it first (named, or into its project).
    const previewUrl = () => `/projects/${encodeURIComponent(currentDesignProject.id)}/deploy-preview/${encodeURIComponent(deploymentHostPicker.value)}`;
    // A project on hold is resumed exactly as it was deployed - the canvas must not overwrite it.
    let heldAlready = false;
    if (deploymentNameMode() === "existing") heldAlready = (await settingsRequest(previewUrl())).held;
    if (!heldAlready) {
      deploymentStatus.textContent = "Saving the design…";
      await saveDesignProject(deploymentNameMode() === "new" ? name : null);
    }
    deploymentPreview = await settingsRequest(previewUrl());
    renderDeploymentPreview(deploymentPreview);
    if (deploymentPreview.mode === "full" && !deploymentPreview.held && !await showConfirm(
      `“${deploymentPreview.project.name}” will be deployed in FULL mode: the topology is rebuilt and managed devices that are not in it may be removed from ${deploymentPreview.host.esxi_ip || deploymentPreview.host.filename}. Continue?`,
      { title: "Full deployment", confirmLabel: "Deploy", danger: true }
    )) {
      deploymentConfirm.disabled = false;
      return;
    }
    if (deploymentPreview.requires_restart) {
      await settingsRequest(`/settings-profiles/${encodeURIComponent(deploymentPreview.host.filename)}/project`, {
        method:"PUT", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({project_id: currentDesignProject.id})
      });
      sessionStorage.setItem("pendingLibraryDeployment", JSON.stringify({project:currentDesignProject, filename:deploymentPreview.host.filename, resume: !!deploymentPreview.held}));
      deploymentStatus.textContent = "Restarting with the selected host and project…";
      try { await fetch(`/settings-profiles/${encodeURIComponent(deploymentPreview.host.filename)}/activate`, {method:"POST"}); } catch (error) {}
      const deadline = Date.now() + 30000;
      while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        try { if ((await fetch("/health")).ok) { window.location.reload(); return; } } catch (error) {}
      }
      throw new Error("Server did not return after restart");
    }
    if (deploymentPreview.held) {
      deploymentDialog.close();
      await enterHost(deploymentPreview.host.filename, true);
      await resumeHeldProject(currentDesignProject);
      return;
    }
    await startLibraryDeployment(currentDesignProject, deploymentPreview.host.filename);
  } catch (error) {
    deploymentStatus.textContent = error.message;
    deploymentConfirm.disabled = false;
  }
});

async function resumePendingLibraryDeployment() {
  const raw = sessionStorage.getItem("pendingLibraryDeployment");
  if (!raw) return;
  sessionStorage.removeItem("pendingLibraryDeployment");
  try {
    const pending = JSON.parse(raw);
    if (pending.resume) {
      await enterHost(pending.filename, true);
      await resumeHeldProject(pending.project);
      return;
    }
    await settingsRequest(`/design/project/${encodeURIComponent(pending.project.id)}/open`, {method:"POST"});
    await enterDesignMode();
    await startLibraryDeployment(pending.project, pending.filename);
  } catch (error) {
    await showAlert(`Deployment could not resume after restart: ${error.message}`, {title:"Deploy project"});
  }
}
resumePendingLibraryDeployment();

// Warning banner > "Deploy project…": deploys the library project that is
// selected for the active host but missing in GNS3 - the same job as
// File > Deploy to Host, minus the host picker (the host is the active one).
document.getElementById("warning-deploy-btn").addEventListener("click", async () => {
  const target = warningDeployProject;
  if (!target) return;
  const button = document.getElementById("warning-deploy-btn");
  button.disabled = true;
  try {
    const preview = await settingsRequest(`/projects/${encodeURIComponent(target.id)}/deploy-preview/${encodeURIComponent(target.filename)}`);
    const impact = preview.mode === "full"
      ? "Full deployment: rebuilds the topology and may remove managed devices that are not in this project."
      : "Incremental deployment: reuses matching devices and removes nothing.";
    const confirmed = await showConfirm(
      `Deploy “${preview.project.name}” to GNS3 on ${preview.host.esxi_ip || preview.host.filename}?\n\n` +
      `${preview.node_count} nodes, ${preview.edge_count} links.\n${impact}`,
      { title: "Deploy project", confirmLabel: "Deploy", danger: preview.mode === "full" }
    );
    if (!confirmed) return;
    const body = await settingsRequest(`/projects/${encodeURIComponent(target.id)}/deploy/${encodeURIComponent(target.filename)}`, { method: "POST" });
    await monitorLibraryDeployment(preview.project, body.job_id);
    // The server resolved "no such GNS3 project" at startup - look again now that it exists.
    await fetch("/reload", { method: "POST" });
    await loadCanvas();
  } catch (error) {
    await showAlert(`Deployment failed: ${error.message}`, { title: "Deploy project" });
  } finally {
    button.disabled = false;
  }
});


// Delete a project from the library. Only the library entry goes - a
// deployment of it in GNS3/ESXi stays untouched (the server also refuses while
// a host has it selected or it is on hold).
document.getElementById("project-library-delete").addEventListener("click", async () => {
  const id = projectPicker.value;
  if (!id || id === "external" || projectLibraryBusy) return;
  const name = projectPicker.selectedOptions[0].textContent;
  if (!await showConfirm(
    `Delete “${name}” from the project library?\n\nThis removes its topology and design from the library only. ` +
    `Whatever is already deployed in GNS3 or on ESXi stays as it is. It cannot be undone.`,
    { title: "Delete project", confirmLabel: "Delete", danger: true }
  )) return;
  projectBusy(true);
  try {
    await settingsRequest(`/projects/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (currentDesignProject && currentDesignProject.id === id) currentDesignProject = null;
    await refreshProjectLibrary("");
    projectSelectionStatus.textContent = `Deleted “${name}”.`;
  } catch (error) { projectSelectionStatus.textContent = error.message; }
  finally { projectBusy(false); }
});

// --- Hold / resume (see app/holds.py) ---

document.getElementById("hold-project-btn").addEventListener("click", async () => {
  if (!await showConfirm(
    "Put this project on hold?\n\nIts ESXi VMs are powered off and its GNS3 project is closed (which stops every node). " +
    "Both are kept and protected from later deployments - nothing is deleted. Resume it any time from the banner or the project dialog.",
    { title: "Hold project", confirmLabel: "Hold" }
  )) return;
  setMenubarBusy("Putting the project on hold…");
  try {
    await settingsRequest("/project/hold", { method: "POST" });
    await fetch("/reload", { method: "POST" });
    await loadCanvas();
    setMenubarMessage("Project is on hold. Use Resume project to start it again.");
  } catch (error) {
    clearMenubarStatus();
    await showAlert(`Couldn't put the project on hold: ${error.message}`, { title: "Hold project" });
  }
});

async function resumeHeldProject(project) {
  setMenubarBusy(`Resuming “${project.name}”… (starting nodes and VMs, this can take a few minutes)`);
  try {
    const body = await settingsRequest(`/projects/${encodeURIComponent(project.id)}/resume`, { method: "POST" });
    await loadCanvas();
    const report = body.report || {};
    setMenubarMessage(
      `Resumed “${project.name}”. Health check: ${report.passed}/${report.total} checks passed.${describePush(body.push_results)}`,
      { error: report.passed !== report.total || pushHadFailures(body.push_results) }
    );
    showPushResults(body.push_results);
    if (body.push_job_id) {
      // The project pushes its configs by itself after a resume; it runs in the background.
      showTab("push");
      const results = await followPushJob(body.push_job_id);
      if (results) {
        const failed = results.filter((r) => !r.ok).length;
        setMenubarMessage(`Resumed “${project.name}” and pushed configs to ${results.length} device${results.length === 1 ? "" : "s"}${failed ? `, ${failed} failed` : ""}.`, { error: failed > 0 });
      }
    }
  } catch (error) {
    clearMenubarStatus();
    await showAlert(`Couldn't resume: ${error.message}`, { title: "Resume project" });
  }
}

document.getElementById("warning-resume-btn").addEventListener("click", async () => {
  const target = warningHeldProject;
  if (!target) return;
  const button = document.getElementById("warning-resume-btn");
  button.disabled = true;
  try { await resumeHeldProject(target); } finally { button.disabled = false; }
});
