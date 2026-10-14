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
    const label = counts.get(project.name) > 1 ? `${project.name} (${project.id.slice(0, 8)})` : project.name;
    projectPicker.add(new Option(label, project.id));
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

document.getElementById("save-library-project-btn").addEventListener("click", async () => {
  if (!currentDesignProject) {
    await openProjectSelection();
    projectSelectionStatus.textContent = "Create a project, then open it in the designer before saving.";
    return;
  }
  setMenubarBusy(`Saving “${currentDesignProject.name}”…`);
  try {
    const body = await settingsRequest(`/design/project/${encodeURIComponent(currentDesignProject.id)}`, { method:"PUT" });
    currentDesignProject = body.project;
    setMenubarMessage(`Saved “${currentDesignProject.name}” to the project library.`);
  } catch (error) { setMenubarMessage(`Save failed: ${error.message}`); }
});

const deploymentDialog = document.getElementById("deployment-preview-dialog");
const deploymentHostPicker = document.getElementById("deployment-host-picker");
const deploymentContent = document.getElementById("deployment-preview-content");
const deploymentStatus = document.getElementById("deployment-preview-status");
const deploymentConfirm = document.getElementById("deployment-confirm");
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
  deploymentStatus.textContent = activation + impact;
  deploymentStatus.className = preview.mode === "full" ? "deployment-warning" : "";
  deploymentConfirm.textContent = preview.requires_restart ? "Prepare and deploy" : "Deploy now";
  deploymentConfirm.disabled = false;
}

async function loadDeploymentPreview() {
  deploymentConfirm.disabled = true;
  deploymentStatus.textContent = "Loading preview…";
  try {
    const preview = await settingsRequest(`/projects/${encodeURIComponent(currentDesignProject.id)}/deploy-preview/${encodeURIComponent(deploymentHostPicker.value)}`);
    renderDeploymentPreview(preview);
  } catch (error) { deploymentStatus.textContent = error.message; }
}

async function openDeploymentPreview() {
  await refreshCurrentDesignProject();
  if (!currentDesignProject) {
    setMenubarMessage("Save this design to the project library before deploying it.");
    return;
  }
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
  setMenubarMessage(`Deployed “${project.name}” successfully.`);
}

async function startLibraryDeployment(project, filename) {
  const body = await settingsRequest(`/projects/${encodeURIComponent(project.id)}/deploy/${encodeURIComponent(filename)}`, { method:"POST" });
  deploymentDialog.close();
  await monitorLibraryDeployment(project, body.job_id);
}

document.getElementById("deploy-library-project-btn").addEventListener("click", openDeploymentPreview);
deploymentHostPicker.addEventListener("change", loadDeploymentPreview);
document.getElementById("deployment-preview-close").addEventListener("click", () => deploymentDialog.close());
deploymentConfirm.addEventListener("click", async () => {
  if (!deploymentPreview || deploymentConfirm.disabled) return;
  deploymentConfirm.disabled = true;
  try {
    if (deploymentPreview.requires_restart) {
      await settingsRequest(`/settings-profiles/${encodeURIComponent(deploymentPreview.host.filename)}/project`, {
        method:"PUT", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({project_id: currentDesignProject.id})
      });
      sessionStorage.setItem("pendingLibraryDeployment", JSON.stringify({project:currentDesignProject, filename:deploymentPreview.host.filename}));
      deploymentStatus.textContent = "Restarting with the selected host and project…";
      try { await fetch(`/settings-profiles/${encodeURIComponent(deploymentPreview.host.filename)}/activate`, {method:"POST"}); } catch (error) {}
      const deadline = Date.now() + 30000;
      while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        try { if ((await fetch("/health")).ok) { window.location.reload(); return; } } catch (error) {}
      }
      throw new Error("Server did not return after restart");
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
    await settingsRequest(`/design/project/${encodeURIComponent(pending.project.id)}/open`, {method:"POST"});
    await enterDesignMode();
    await startLibraryDeployment(pending.project, pending.filename);
  } catch (error) {
    await showAlert(`Deployment could not resume after restart: ${error.message}`, {title:"Deploy project"});
  }
}
resumePendingLibraryDeployment();
