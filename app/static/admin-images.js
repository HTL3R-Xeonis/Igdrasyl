"use strict";
// [admin-images.js] The admin panel's Images section: how the automated appliance image build is doing.
// The page only reads (GET /admin/image-builds, app/routes/image_builds.py): the gateway asks the build machine's status page and
// leaves one view. Nothing here changes anything or sends an alarm. Loaded before admin.js, which calls loadImageBuilds().

const imagesURL = "/admin/image-builds";
const IMAGES_REFRESH_MS = 60000;
let imagesTimer = null;

const imagesEl = (id) => document.getElementById(id);

function imagesAge(seconds) {
  if (seconds === null || seconds === undefined) return "";
  if (seconds < 90) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `${minutes} minutes ago`;
  const hours = Math.round(seconds / 3600);
  if (hours < 48) return `${hours} hours ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

function imagesTime(stamp) {
  const date = stamp ? new Date(stamp) : null;
  return date && !Number.isNaN(date.getTime()) ? date.toLocaleString() : "-";
}

function imagesState(view) {
  const worker = view.worker ? ` Build machine: ${view.worker}.` : "";
  if (view.state === "building") {
    const run = view.active || {};
    return `A build is running${run.release ? ` for ${run.release}` : ""}${run.phase ? ` (${run.phase})` : ""}.${worker}`;
  }
  if (view.state === "paused") return `Builds are paused.${worker}`;
  return `No build is running.${worker}`;
}

function imagesHeadline(view) {
  const latest = view.latest;
  if (!latest) return "No build has run yet";
  const age = imagesAge(latest.age_seconds);
  return `Last build ${latest.version}: ${latest.result}${age ? `, ${age}` : ""}`;
}

function renderImageRows(builds) {
  const body = imagesEl("ap-images-body");
  body.replaceChildren();
  for (const build of builds) {
    const tr = document.createElement("tr");
    tr.insertCell().textContent = build.version;
    tr.insertCell().textContent = build.result;
    tr.insertCell().textContent = imagesTime(build.finished_at);
    tr.insertCell().textContent = build.acceptance ? `${build.acceptance.passed} of ${build.acceptance.total}` : "-";
    tr.insertCell().textContent = build.reason || "-";
    if (["failed", "blocked", "interrupted"].includes(build.result)) tr.className = "ap-images-bad";
    body.append(tr);
  }
}

function renderImageBuilds(view) {
  const unavailable = imagesEl("ap-images-unavailable");
  const card = imagesEl("ap-images-card");
  if (!view.configured || !view.reachable) {
    card.hidden = true;
    setStatus("ap-images-unavailable", view.message, view.health === "unreachable" || view.health === "invalid");
    return;
  }
  unavailable.textContent = "";
  unavailable.classList.remove("ap-error");
  card.hidden = false;
  imagesEl("ap-images-dot").className = `ap-images-dot ap-images-${view.health}`;
  imagesEl("ap-images-headline").textContent = imagesHeadline(view);
  imagesEl("ap-images-detail").textContent = `${view.message} ${imagesState(view)} Checked ${imagesTime(view.checked_at)}.`;
  renderImageRows(view.builds);
}

function imagesSchedule() {
  if (imagesTimer) clearTimeout(imagesTimer);
  imagesTimer = setTimeout(() => {
    // Only ask while the section is on screen: the build machine is not bothered by an admin who looks at another section.
    if (location.hash === "#images" && !document.hidden) loadImageBuilds();
    else imagesSchedule();
  }, IMAGES_REFRESH_MS);
}

async function loadImageBuilds() {
  try {
    renderImageBuilds(await request(imagesURL));
  } catch (error) {
    imagesEl("ap-images-card").hidden = true;
    setStatus("ap-images-unavailable", error.message, true);
  }
  imagesSchedule();
}

window.addEventListener("hashchange", () => {
  if (location.hash === "#images") loadImageBuilds();
});
document.addEventListener("DOMContentLoaded", () => {
  imagesEl("ap-images-refresh").addEventListener("click", loadImageBuilds);
});
