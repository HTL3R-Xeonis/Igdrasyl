// Design mode: a "Deploy" button next to the "Design mode" badge that deploys the saved design to the selected host with
// no dialog and no confirmation. It reuses the exact calls of the "Deploy to Host…" dialog (projects.js): save the edits to
// the design's project, preview, restart into the project when the host needs it, resume a held project, start the job.
// Progress and the result use the menubar status line (setMenubarBusy / setMenubarMessage), as every other deployment.
(function () {
  const button = document.getElementById("design-deploy-btn");
  const reason = document.getElementById("design-deploy-reason");
  let deploying = false;

  function isReadOnly() {
    return Boolean(window.currentUser) && window.currentUser.role === "readonly";
  }

  function refreshDesignDeployButton() {
    const visible = designMode && !isReadOnly();
    button.hidden = !visible;
    if (!visible) { reason.hidden = true; return; }
    const missing = Object.keys(nodesData).length ? "" : "Add a device first";
    button.disabled = deploying || Boolean(missing);
    button.textContent = deploying ? "Deploying…" : "Deploy";
    reason.textContent = missing;
    reason.hidden = !missing;
  }

  async function deployDesignNow() {
    if (deploying || button.disabled) return;
    deploying = true;
    refreshDesignDeployButton();
    try {
      await refreshCurrentDesignProject();
      // The dialog asks for a name when the design was never saved: the one value that cannot be guessed.
      if (!currentDesignProject) { await openDeploymentPreview(); return; }
      const hosts = (await settingsRequest("/settings-profiles")).filter((profile) => !profile.error);
      if (!hosts.length) {
        setMenubarMessage("Add a valid host before deploying.", { error: true });
        return;
      }
      // The host the user works on. With several hosts and none selected a choice is unavoidable: the dialog asks.
      const host = hosts.find((profile) => profile.active) || (hosts.length === 1 ? hosts[0] : null);
      if (!host) { await openDeploymentPreview(); return; }
      const previewUrl = () => `/projects/${encodeURIComponent(currentDesignProject.id)}/deploy-preview/${encodeURIComponent(host.filename)}`;
      setMenubarBusy("Checking the project…");
      let preview = await settingsRequest(previewUrl());
      if (!preview.held) {
        // A project on hold is resumed as it was deployed; otherwise the canvas is saved into the project first.
        setMenubarBusy("Saving the design…");
        await saveDesignProject(null);
        preview = await settingsRequest(previewUrl());
      }
      if (preview.requires_restart) { await restartIntoProject(preview); return; }
      if (preview.held) {
        await enterHost(preview.host.filename, true);
        await resumeHeldProject(currentDesignProject);
        return;
      }
      await startLibraryDeployment(currentDesignProject, preview.host.filename);
    } catch (error) {
      setMenubarMessage(`Deployment failed: ${error.message}`, { error: true });
    } finally {
      deploying = false;
      refreshDesignDeployButton();
    }
  }

  // The button follows the mode and the node count without touching the files that change them.
  const applyMode = applyDesignModeUI;
  applyDesignModeUI = function () { applyMode(); refreshDesignDeployButton(); };
  const drawSummary = renderSummary;
  renderSummary = function () { drawSummary(); refreshDesignDeployButton(); };
  document.addEventListener("currentuser", refreshDesignDeployButton);
  button.addEventListener("click", deployDesignNow);
  refreshDesignDeployButton();
})();
