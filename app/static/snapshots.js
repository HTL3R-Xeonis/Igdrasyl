// [snapshots.js] Named snapshots of a bound library project's design
// (app/project_snapshots.py) - a checkpoint under the project you are
// already working on, distinct from Save Project (overwrites it) and
// Save Project As (creates a new, separate one). Only meaningful once
// a project is bound (currentDesignProject, see projects.js).
const snapshotsDialog = document.getElementById("snapshots-dialog");

function snapshotsStatus(text) {
  document.getElementById("snapshots-status").textContent = text || "";
}

async function renderSnapshotsList() {
  const list = document.getElementById("snapshots-list");
  list.innerHTML = "";
  const { snapshots } = await settingsRequest(
    `/design/project/${encodeURIComponent(currentDesignProject.id)}/snapshots`
  );
  for (const snapshot of snapshots) {
    const li = document.createElement("li");
    const when = new Date(snapshot.created_at).toLocaleString();
    li.textContent = `${snapshot.name} (${when}) `;
    const restoreBtn = document.createElement("button");
    restoreBtn.className = "toolbar-btn";
    restoreBtn.type = "button";
    restoreBtn.textContent = "Restore";
    restoreBtn.addEventListener("click", () => restoreSnapshot(snapshot));
    const deleteBtn = document.createElement("button");
    deleteBtn.className = "toolbar-btn";
    deleteBtn.type = "button";
    deleteBtn.textContent = "Delete";
    deleteBtn.addEventListener("click", () => deleteSnapshot(snapshot));
    li.append(restoreBtn, deleteBtn);
    list.appendChild(li);
  }
  if (!snapshots.length) {
    const li = document.createElement("li");
    li.textContent = "No snapshots yet.";
    list.appendChild(li);
  }
}

document.getElementById("project-snapshots-btn").addEventListener("click", async () => {
  if (!currentDesignProject) {
    await showAlert("Save this sketch as a project first (File > Save Project), then snapshots can checkpoint it.", { title: "Snapshots" });
    return;
  }
  snapshotsStatus("");
  snapshotsDialog.showModal();
  try {
    await renderSnapshotsList();
  } catch (error) {
    snapshotsStatus(error.message);
  }
});

document.getElementById("snapshot-create-form").addEventListener("submit", async (evt) => {
  evt.preventDefault();
  const nameInput = document.getElementById("snapshot-name");
  const name = nameInput.value.trim();
  if (!name) return;
  try {
    await settingsRequest(
      `/design/project/${encodeURIComponent(currentDesignProject.id)}/snapshots`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) }
    );
    nameInput.value = "";
    snapshotsStatus("");
    await renderSnapshotsList();
  } catch (error) {
    snapshotsStatus(error.message);
  }
});

async function restoreSnapshot(snapshot) {
  const ok = await showConfirm(
    `Replace the current sketch with the snapshot "${snapshot.name}"? Any unsaved changes since the last save/snapshot are lost (Edit > Undo can still bring them back).`,
    { title: "Restore snapshot", confirmLabel: "Restore", danger: true }
  );
  if (!ok) return;
  try {
    await settingsRequest(
      `/design/project/${encodeURIComponent(currentDesignProject.id)}/snapshots/${encodeURIComponent(snapshot.id)}/restore`,
      { method: "POST" }
    );
  } catch (error) {
    snapshotsStatus(error.message);
    return;
  }
  snapshotsDialog.close();
  deselectNode();
  drawingsLoaded = false;
  await loadCanvas();
  refreshHistoryState();
}

async function deleteSnapshot(snapshot) {
  const ok = await showConfirm(`Delete the snapshot "${snapshot.name}"? This cannot be undone.`, { title: "Delete snapshot", confirmLabel: "Delete", danger: true });
  if (!ok) return;
  try {
    await settingsRequest(
      `/design/project/${encodeURIComponent(currentDesignProject.id)}/snapshots/${encodeURIComponent(snapshot.id)}`,
      { method: "DELETE" }
    );
    await renderSnapshotsList();
  } catch (error) {
    snapshotsStatus(error.message);
  }
}

document.getElementById("snapshots-close").addEventListener("click", () => snapshotsDialog.close());
