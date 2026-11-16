// The gateway handles this check even when the caller has no host selected.
async function deployRequest(url, options = {}) {
  const response = await fetch(url, { ...options, cache: "no-store" });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status}).`);
  return body;
}

for (const button of document.querySelectorAll("[data-deploy-profile]")) {
  button.addEventListener("click", async () => {
    const filename = button.dataset.deployProfile;
    if (!window.confirm(`Check ${filename} and provision its GNS3 VM if it is missing? This can take several minutes.`)) return;

    const status = button.parentElement.querySelector("[data-deploy-status]");
    const url = `/settings-profiles/${encodeURIComponent(filename)}/deploy-gns3-vm`;
    button.disabled = true;
    status.textContent = "Waiting for the host backend…";
    try {
      const { job_id } = await deployRequest(url, { method: "POST" });
      while (true) {
        const result = await deployRequest(`${url}/${encodeURIComponent(job_id)}`);
        if (result.status === "ready") {
          status.textContent = `GNS3 VM ready at ${result.gns3_vm_ip}.`;
          break;
        }
        if (result.status === "unavailable") {
          status.textContent = result.warning || "The backend is running. The GNS3 VM has no IP address yet.";
          break;
        }
        await new Promise(resolve => setTimeout(resolve, 1200));
      }
    } catch (error) {
      status.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
}
