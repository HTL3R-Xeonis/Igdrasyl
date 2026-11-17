const shareURL = "/admin/template-share";
const shareDialog = document.getElementById("ap-share-dialog");
const shareForm = document.getElementById("ap-share-form");
const shareProgress = document.getElementById("ap-share-progress");
const shareSave = document.getElementById("ap-share-save");
const shareCancel = document.getElementById("ap-share-cancel");
const shareField = name => shareForm.elements.namedItem(name);
let shareEditing = null;
let shareUpload = null;
let shareSaving = false;

async function loadTemplateShare() {
  const actions = document.getElementById("ap-share-actions");
  const table = document.getElementById("ap-share-table");
  const body = document.getElementById("ap-share-body");
  try {
    const share = await request(shareURL);
    actions.hidden = !share.enabled;
    table.hidden = !share.enabled || !share.templates.length;
    body.replaceChildren();
    for (const entry of share.templates) {
      const row = document.createElement("tr");
      for (const value of [
        entry.metadata.name, entry.metadata.category,
        entry.file_exists ? [entry.file, entry.size].filter(Boolean).join(" · ") : "File missing",
      ]) {
        row.insertCell().textContent = value || "—";
      }
      if (entry.id) {
        const edit = document.createElement("button");
        edit.type = "button";
        edit.className = "toolbar-btn";
        edit.textContent = "Edit…";
        edit.setAttribute("aria-label", "Edit " + entry.metadata.name);
        edit.addEventListener("click", () => openTemplateDialog(entry));
        row.insertCell().append(edit);
      } else {
        row.insertCell();
      }
      body.append(row);
    }
    setStatus("ap-share-status", !share.enabled
      ? "Enable template management in the appliance setup to use this share."
      : share.templates.length ? "" : "No templates found. Import an OVA to add one.");
  } catch (error) {
    actions.hidden = table.hidden = true;
    setStatus("ap-share-status", error.message, true);
  }
}

function openTemplateDialog(entry = null) {
  shareEditing = entry;
  shareForm.reset();
  shareProgress.hidden = true;
  shareField("name").readOnly = Boolean(entry);
  shareField("ova").required = !entry;
  document.getElementById("ap-share-file-label").hidden = Boolean(entry);
  document.getElementById("ap-share-users-label").hidden = Boolean(entry && entry.metadata.users === null);
  document.getElementById("ap-share-title").textContent = entry ? "Edit template metadata" : "Import ESXi template";
  shareSave.textContent = entry ? "Save" : "Import";
  for (const [key, value] of Object.entries(entry?.metadata || {})) {
    if (shareField(key)) shareField(key).value = Array.isArray(value) ? value.join(", ") : value ?? "";
  }
  setStatus("ap-share-form-status", "");
  shareDialog.showModal();
}

function templateMetadata() {
  const fields = ["name", "category", "os_family", "os_version", "memory", "disk", "hostname", "domain", "note"];
  const metadata = Object.fromEntries(fields.map(key => [key, shareField(key).value.trim()]));
  const list = key => shareField(key).value.split(",").map(item => item.trim()).filter(Boolean);
  metadata.cpu = Number(shareField("cpu").value);
  metadata.domain = metadata.domain || null;
  metadata.tags = list("tags");
  if (!shareEditing || shareEditing.metadata.users !== null) metadata.users = list("users");
  return metadata;
}

function uploadTemplate(file, metadata) {
  return new Promise((resolve, reject) => {
    const upload = new XMLHttpRequest();
    shareUpload = upload;
    upload.open("POST", shareURL + "/import");
    upload.responseType = "json";
    upload.setRequestHeader("Content-Type", "application/octet-stream");
    upload.setRequestHeader("X-Template-Metadata", encodeURIComponent(JSON.stringify(metadata)));
    shareProgress.hidden = false;
    shareProgress.removeAttribute("value");
    upload.upload.onprogress = event => {
      if (!event.lengthComputable) return;
      shareProgress.value = 100 * event.loaded / event.total;
      if (event.loaded === event.total) {
        shareCancel.disabled = true;
        setStatus("ap-share-form-status", "Validating the OVA and writing its YAML…");
      }
    };
    upload.onload = () => {
      if (upload.status >= 200 && upload.status < 300) return resolve(upload.response);
      const detail = upload.response?.detail;
      const message = Array.isArray(detail)
        ? detail.map(error => error.loc.join(".") + " — " + error.msg).join("; ")
        : detail;
      reject(new Error(message || "Upload failed (" + upload.status + ")."));
    };
    upload.onerror = () => reject(new Error("The upload connection was interrupted."));
    upload.onabort = () => reject(new Error("Upload cancelled."));
    upload.send(file);
  });
}

document.getElementById("ap-share-import").addEventListener("click", () => openTemplateDialog());
document.getElementById("ap-share-refresh").addEventListener("click", loadTemplateShare);
shareField("ova").addEventListener("change", () => {
  const file = shareField("ova").files[0];
  if (file && !shareField("name").value.trim()) {
    shareField("name").value = file.name.replace(/(?:_[a-f0-9]{64})?\.ova$/i, "").slice(0, 120);
  }
});
shareCancel.addEventListener("click", () => {
  if (shareUpload) shareUpload.abort();
  if (!shareSaving || shareUpload) shareDialog.close();
});
shareDialog.addEventListener("cancel", event => {
  if (shareUpload && !shareCancel.disabled) shareUpload.abort();
  else if (shareSaving) event.preventDefault();
});
shareForm.addEventListener("submit", async event => {
  event.preventDefault();
  if (shareSaving) return;
  shareSaving = shareSave.disabled = true;
  const editing = shareEditing;
  shareCancel.disabled = Boolean(editing);
  const metadata = templateMetadata();
  setStatus("ap-share-form-status", editing ? "Saving…" : "Uploading OVA…");
  try {
    const result = editing
      ? await request(shareURL, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: editing.id, manifest: editing.manifest, revision: editing.revision, metadata }),
      })
      : await uploadTemplate(shareField("ova").files[0], metadata);
    shareDialog.close();
    await Promise.all([loadTemplateShare(), loadAuditLog()]);
    const refresh = result.refresh_minutes
      ? " The catalog refreshes every " + result.refresh_minutes + " min."
      : " The catalog updates on its next refresh.";
    setStatus("ap-share-status", result.name + (editing ? " saved." : " imported.") + refresh);
  } catch (error) {
    if (shareDialog.open) setStatus("ap-share-form-status", error.message, true);
  } finally {
    shareUpload = null;
    shareSaving = shareSave.disabled = false;
    shareCancel.disabled = false;
    shareProgress.hidden = true;
  }
});
