"use strict";
// One admin section at a time, chosen by the URL hash (#users, #ai, #logs, #templates, #audit).
(() => {
  const panels = [...document.querySelectorAll("section.ap-panel")];
  const links = [...document.querySelectorAll("#ap-nav a")];
  const known = panels.map((panel) => panel.dataset.panel);

  function show() {
    const wanted = location.hash.slice(1);
    const name = known.includes(wanted) ? wanted : known[0];
    panels.forEach((panel) => panel.classList.toggle("ap-active", panel.dataset.panel === name));
    links.forEach((link) => {
      if (link.dataset.panel === name) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
  }

  window.addEventListener("hashchange", show);
  show();
})();
