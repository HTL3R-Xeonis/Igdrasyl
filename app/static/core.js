// [core.js] Shared helpers and the terminal / VM-screen session panels: SVG namespace, tab pop-out, confirm and dropdown dialogs. Loaded in the order listed in index.html; all scripts share one global scope.
const SVG_NS = "http://www.w3.org/2000/svg";

// Multiple simultaneous console/screen sessions, shown as tabs next to
// each other (like a terminal's own tabs) instead of one replacing
// another. Each session keeps its own live connection - a WebSocket +
// xterm.js Terminal, or an RFB instance - running even while its tab
// isn't the active one: switching only toggles which panel is visible
// ([hidden]), nothing is ever torn down or recreated by a switch, so a
// background tab keeps receiving real data and is instantly current
// when switched back to.
const VM_SCREEN_MAX_CONSECUTIVE_FAILURES = 8;
let sessions = new Map();   // id -> session object, see ensureSessionPanel
let activeSessionId = null;

// The Console/Screen view can be popped out into its own window or tab (see the Pop out button below). Only these parts
// move there; the context menus (#node-actions and friends) are position:fixed popups of THIS page and stay in it.
const POP_OUT_PARTS = ["session-tabrow", "console-placeholder", "session-panels"];
let poppedOutWindow = null;

// An element of the console view, wherever it is right now: in this page, or in the pop-out's document.
function consoleEl(id) {
  const doc = poppedOutWindow && !poppedOutWindow.closed ? poppedOutWindow.document : document;
  return doc.getElementById(id);
}

function sessionIdFor(nodeName, kind) {
  return `${kind}:${nodeName}`;
}

// Creates a session's tab + panel the first time id is seen, or just
// returns the existing one - callers check the returned session's own
// `.kind` to tell "brand new, still needs to be opened" (null) apart
// from "already open or opening" (set), so re-clicking a node's own
// Console/Screen button switches to its existing tab instead of
// starting a duplicate connection.
function ensureSessionPanel(id, nodeName, title) {
  let session = sessions.get(id);
  if (session) return session;

  const tab = document.createElement("button");
  tab.className = "session-tab";
  tab.type = "button";
  const label = document.createElement("span");
  label.className = "session-tab-label";
  label.textContent = title;
  // the connection state sits beside the name (a failure is coloured, the full text is the tooltip)
  const statusEl = document.createElement("span");
  statusEl.className = "session-tab-status";
  const close = document.createElement("span");
  close.className = "session-tab-close";
  close.textContent = "×";
  close.title = "Close";
  close.addEventListener("click", (evt) => { evt.stopPropagation(); closeSession(id); });
  tab.append(label, statusEl, close);
  tab.addEventListener("click", () => onSessionTabClick(id));
  tab.addEventListener("pointerdown", (evt) => startTabDrag(evt, id));
  consoleEl("session-tabbar").appendChild(tab);
  consoleEl("session-tabbar").hidden = false;

  const panel = document.createElement("div");
  panel.className = "session-panel"; // starts inactive (see the CSS - default is the :not(.active) rule) until switchToSession adds .active
  const body = document.createElement("div");
  body.className = "session-panel-body";
  panel.append(body);
  consoleEl("session-panels").appendChild(panel);

  consoleEl("console-placeholder").style.display = "none";

  session = {
    id, nodeName, title, kind: null, popout: null,
    tabEl: tab, panelEl: panel, statusEl, bodyEl: body,
    ws: null, term: null, rfb: null,
    screenWanted: false, reconnectAttempts: 0,
  };
  sessions.set(id, session);
  return session;
}

function setSessionStatus(session, text, dotClass) {
  session.statusEl.textContent = text;
  session.statusEl.title = text;
  session.statusEl.className = `session-tab-status ${dotClass || ""}`.trim();
}

function switchToSession(id) {
  activeSessionId = id;
  for (const s of sessions.values()) {
    // a session in its own window keeps its panel active (visible) there; its tab here just points to the window
    const popped = isPopped(s);
    const active = !popped && s.id === id;
    s.panelEl.classList.toggle("active", popped || active);
    s.tabEl.classList.toggle("active", active);
  }
}

function closeSession(id) {
  const session = sessions.get(id);
  if (!session) return;
  session.screenWanted = false;
  if (session.ws) session.ws.close();
  if (session.rfb) session.rfb.disconnect();
  const popout = session.popout;
  session.tabEl.remove();
  session.panelEl.remove();
  sessions.delete(id);
  if (popout && !popout.closed) popout.close();
  if (activeSessionId !== id) return;
  const next = [...sessions.values()].find((s) => !isPopped(s));
  if (next) {
    switchToSession(next.id);
  } else {
    activeSessionId = null;
    if (!sessions.size) consoleEl("session-tabbar").hidden = true;
    consoleEl("console-placeholder").style.display = "";
  }
}

// ---- tab drag-out (pure geometry, no DOM; tests/test_tab_dragout.py runs this block under node) ----
// Pressing a session tab and moving the pointer at least TAB_DRAG_THRESHOLD px is a drag (less is a click); a drag that is
// released more than TAB_DRAG_OUT_MARGIN px outside the Console/Screen area opens that session in its own window.
const TAB_DRAG_THRESHOLD = 8;
const TAB_DRAG_OUT_MARGIN = 24;
function tabDragState(start, point, area) {
  const dragging = Math.hypot(point.x - start.x, point.y - start.y) >= TAB_DRAG_THRESHOLD;
  const outside =
    point.x < area.left - TAB_DRAG_OUT_MARGIN ||
    point.x > area.right + TAB_DRAG_OUT_MARGIN ||
    point.y < area.top - TAB_DRAG_OUT_MARGIN ||
    point.y > area.bottom + TAB_DRAG_OUT_MARGIN;
  return { dragging, outside: dragging && outside };
}
// Dragging a tab along the tab row: the index it takes when the pointer is at x. `rects` are the tabs' {left, right} in their
// current order and `from` is the dragged tab's index; a tab is passed when the pointer crosses the middle of its neighbour.
function tabReorderIndex(x, rects, from) {
  const middle = (r) => (r.left + r.right) / 2;
  for (let i = 0; i < from; i++) if (x < middle(rects[i])) return i;
  for (let i = rects.length - 1; i > from; i--) if (x > middle(rects[i])) return i;
  return from;
}
// ---- end tab drag-out ----

function isPopped(session) {
  return !!(session.popout && !session.popout.closed);
}

let suppressTabClick = false; // the click that ends a drag must not also switch tabs

function onSessionTabClick(id) {
  if (suppressTabClick) return;
  const session = sessions.get(id);
  if (!session) return;
  if (isPopped(session)) {
    session.popout.focus();
    return;
  }
  switchToSession(id);
}

// Drag a tab sideways along the row to change the order of the sessions, or out of the panel with the left mouse button to open
// just that session in its own window (the Pop out button does the same for the whole panel). Escape cancels the drag.
// Works in the pop-out window as well: the events then arrive in that window, so the listeners go on the tab's own window.
function startTabDrag(evt, id) {
  const session = sessions.get(id);
  if (!session || evt.button !== 0 || evt.target.closest(".session-tab-close")) return;
  const tab = session.tabEl;
  const view = tab.ownerDocument.defaultView;
  const canPopOut = !isPopped(session) && tab.ownerDocument === document && !(poppedOutWindow && !poppedOutWindow.closed);
  const start = { x: evt.clientX, y: evt.clientY };
  let hint = null;
  let state = { dragging: false, outside: false };
  let finished = false;
  const areaRect = () => tab.ownerDocument.getElementById("console-area").getBoundingClientRect();
  const reorder = (x) => {
    const bar = tab.parentElement;
    const tabs = [...bar.children];
    const from = tabs.indexOf(tab);
    const to = tabReorderIndex(x, tabs.map((t) => t.getBoundingClientRect()), from);
    if (to === from) return;
    bar.insertBefore(tab, to < from ? tabs[to] : tabs[to].nextSibling);
  };
  try {
    tab.setPointerCapture(evt.pointerId); // keeps the events coming while the pointer is outside the browser window
  } catch (err) {
    // not capturable: the window listeners below still work inside the page
  }

  const onMove = (e) => {
    state = tabDragState(start, { x: e.clientX, y: e.clientY }, areaRect());
    if (!state.dragging) return;
    if (!canPopOut || !state.outside) reorder(e.clientX);
    if (!canPopOut) {
      tab.classList.add("dragging-out");
      return;
    }
    if (!hint) {
      hint = document.createElement("div");
      hint.id = "tab-dragout-hint";
      document.body.appendChild(hint);
      tab.classList.add("dragging-out");
    }
    hint.textContent = state.outside ? "Release to open in its own window (Shift: new browser tab)" : "Drag out of the panel to open in its own window (Shift: new browser tab)";
    hint.classList.toggle("will-pop", state.outside);
    hint.style.left = `${Math.max(0, Math.min(e.clientX + 14, window.innerWidth - 300))}px`;
    hint.style.top = `${Math.max(0, Math.min(e.clientY + 14, window.innerHeight - 30))}px`;
  };
  const finish = (e, cancelled) => {
    if (finished) return;
    finished = true;
    view.removeEventListener("pointermove", onMove);
    view.removeEventListener("pointerup", onUp);
    view.removeEventListener("pointercancel", onCancel);
    view.removeEventListener("keydown", onKey, true);
    if (hint) hint.remove();
    tab.classList.remove("dragging-out");
    try {
      tab.releasePointerCapture(evt.pointerId);
    } catch (err) {
      // already released
    }
    if (!cancelled && e.clientX !== undefined) state = tabDragState(start, { x: e.clientX, y: e.clientY }, areaRect());
    if (!state.dragging) return;
    suppressTabClick = true;
    setTimeout(() => { suppressTabClick = false; }, 0);
    if (!cancelled && canPopOut && state.outside) popOutSession(id, e.screenX, e.screenY, e.shiftKey);
  };
  const onUp = (e) => finish(e, false);
  const onCancel = (e) => finish(e, true);
  const onKey = (e) => { if (e.key === "Escape") finish(e, true); };
  view.addEventListener("pointermove", onMove);
  view.addEventListener("pointerup", onUp);
  view.addEventListener("pointercancel", onCancel);
  view.addEventListener("keydown", onKey, true);
}

// Opens one session in its own browser window by moving its live panel there (not a copy), so the terminal or screen and
// its connection keep running uninterrupted. Closing that window brings the session back into its tab.
// A pop-out opens as its own window; with `asTab` as a new browser tab instead (window.open without window features).
// Careful with the tab: the terminal and the VNC screen draw with requestAnimationFrame of THIS page, which a browser pauses
// while this page is a background tab - so a popped-out tab can show a frozen screen. A separate window has no such problem.
function openPopOut(asTab, features) {
  return asTab ? window.open("", "_blank") : window.open("", "_blank", features);
}

function popOutSession(id, screenX, screenY, asTab = false) {
  const session = sessions.get(id);
  if (!session) return;
  if (isPopped(session)) {
    session.popout.focus();
    return;
  }
  const width = 900;
  const height = 650;
  const win = openPopOut(
    asTab,
    `popup=yes,width=${width},height=${height},left=${Math.max(0, Math.round((screenX || 0) - width / 2))},top=${Math.max(0, Math.round((screenY || 0) - 20))}`,
  );
  if (!win) {
    showAlert("Couldn't open a new window or tab - check your browser's popup blocker.");
    return;
  }
  win.document.head.innerHTML = document.head.innerHTML; // first: it also carries this page's own <title>
  win.document.title = `Ginnungagap - ${session.title}`;
  // the same structure as in the panel, so every existing style applies to the moved session
  const area = win.document.createElement("div");
  area.id = "console-area";
  const bar = win.document.createElement("div");
  bar.className = "popout-bar";
  const barTitle = win.document.createElement("span");
  barTitle.textContent = session.title;
  const popIn = win.document.createElement("button");
  popIn.type = "button";
  popIn.className = "popout-pop-in";
  popIn.textContent = "Pop in";
  popIn.title = "Put this session back into its tab";
  popIn.addEventListener("click", () => {
    restoreSession(session, win);
    win.close();
  });
  bar.append(barTitle, popIn);
  const panels = win.document.createElement("div");
  panels.id = "session-panels";
  area.append(bar, panels);
  win.document.body.appendChild(area);
  panels.appendChild(session.panelEl);
  session.panelEl.classList.add("active"); // visible in its own window, whichever tab was active here
  session.popout = win;
  session.tabEl.classList.add("popped");
  win.addEventListener("beforeunload", () => restoreSession(session, win));
  if (activeSessionId === id) {
    // the panel shows another session now, or the placeholder when there is none left
    const remaining = [...sessions.values()].find((s) => !isPopped(s));
    if (remaining) {
      switchToSession(remaining.id);
    } else {
      activeSessionId = null;
      consoleEl("console-placeholder").style.display = "";
    }
  }
}

// A console that is opened (not just switched to): to the front when it is in its own window, or into a window of its own
// when the setting asks for that. It runs inside the click that opened it, so the browser allows the new window.
let openConsolesInWindow = false;
try {
  openConsolesInWindow = localStorage.getItem("ginnungagap.openConsolesInWindow") === "1";
} catch (err) {
  // no storage: the setting just starts off
}
function surfaceSession(session, wasNew) {
  if (isPopped(session)) {
    session.popout.focus();
    return;
  }
  if (wasNew && openConsolesInWindow && !(poppedOutWindow && !poppedOutWindow.closed)) {
    popOutSession(session.id, window.screenX + window.outerWidth / 2, window.screenY + 80);
  }
}

function restoreSession(session, win) {
  if (sessions.get(session.id) !== session || session.popout !== win) return;
  session.popout = null;
  session.tabEl.classList.remove("popped");
  consoleEl("session-panels").appendChild(session.panelEl);
  consoleEl("console-placeholder").style.display = "none";
  switchToSession(session.id);
}

window.addEventListener("beforeunload", () => {
  for (const s of sessions.values()) if (isPopped(s)) s.popout.close();
});

function closeSessionsForNode(nodeName) {
  for (const session of [...sessions.values()]) {
    if (session.nodeName === nodeName) closeSession(session.id);
  }
}

// In-app replacements for the browser's own confirm()/alert()/prompt(),
// styled to match the rest of the UI rather than popping an
// OS-chrome dialog. All resolve a Promise instead of blocking the
// thread, so every call site awaits them from an async function.
function openModal(config) {
  return new Promise((resolve) => {
    const overlay = document.getElementById("modal-overlay");
    const titleEl = document.getElementById("modal-title");
    const messageEl = document.getElementById("modal-message");
    const inputEl = document.getElementById("modal-input");
    const textareaEl = document.getElementById("modal-textarea");
    const fieldWrap = document.getElementById("modal-field-wrap");
    const confirmBtn = document.getElementById("modal-confirm-btn");
    const cancelBtn = document.getElementById("modal-cancel-btn");

    const isAlert = config.mode === "alert";
    const isConfirm = config.mode === "confirm";
    const isText = config.mode === "text";
    const isTextarea = config.mode === "textarea";
    const attachmentOptions = config.attachments;
    let attachmentFields;
    let fileInput;
    let fileError;
    if (attachmentOptions) {
      attachmentFields = document.createElement("div");
      const fileLabel = document.createElement("label");
      fileLabel.textContent = "Attachments (optional)";
      fileLabel.htmlFor = "modal-attachments";
      fileInput = document.createElement("input");
      fileInput.id = "modal-attachments";
      fileInput.type = "file";
      fileInput.multiple = true;
      fileInput.accept = attachmentOptions.accept;
      fileError = document.createElement("p");
      fileError.setAttribute("role", "alert");
      fileInput.addEventListener("change", () => {
        fileError.textContent = "";
      });
      attachmentFields.append(fileLabel, fileInput, fileError);
      fieldWrap.appendChild(attachmentFields);
    }

    titleEl.textContent = config.title || "";
    messageEl.textContent = config.message || "";
    messageEl.hidden = !config.message;
    inputEl.hidden = true;
    textareaEl.hidden = true;
    fieldWrap.hidden = !isText && !isTextarea;
    cancelBtn.hidden = isAlert;
    cancelBtn.textContent = config.cancelLabel || "Cancel";
    confirmBtn.textContent = config.confirmLabel || (isConfirm ? "Confirm" : "OK");
    confirmBtn.classList.toggle("danger", !!config.danger);
    confirmBtn.classList.toggle("primary", !config.danger);

    if (isText) {
      inputEl.hidden = false;
      inputEl.value = config.defaultValue || "";
      inputEl.placeholder = config.placeholder || "";
    } else if (isTextarea) {
      textareaEl.hidden = false;
      textareaEl.value = config.defaultValue || "";
      textareaEl.placeholder = config.placeholder || "";
    }

    function finish(result) {
      overlay.hidden = true;
      document.removeEventListener("keydown", onKeydown);
      confirmBtn.onclick = null;
      cancelBtn.onclick = null;
      overlay.onclick = null;
      attachmentFields?.remove();
      resolve(result);
    }
    function doConfirm() {
      if (isAlert) return finish(undefined);
      if (isConfirm) return finish(true);
      if (attachmentOptions) {
        const prompt = textareaEl.value.trim();
        const files = Array.from(fileInput.files);
        const error = attachmentOptions.validate(prompt, files);
        if (error) {
          fileError.textContent = error;
          return;
        }
        return finish({ prompt, files });
      }
      if (isTextarea) return finish(textareaEl.value.trim() || null);
      return finish(inputEl.value.trim() || null);
    }
    function doCancel() {
      if (isAlert) return finish(undefined);
      if (isConfirm) return finish(false);
      return finish(null);
    }
    function onKeydown(evt) {
      if (evt.key === "Escape") { evt.preventDefault(); doCancel(); }
      // A plain Enter in the textarea types a newline instead of
      // submitting - Ctrl/Cmd+Enter submits it, same convention as
      // most chat/comment boxes. Every other mode still submits on a
      // plain Enter.
      else if (evt.key === "Enter" && evt.target === fileInput) return;
      else if (evt.key === "Enter" && isTextarea && !(evt.ctrlKey || evt.metaKey)) return;
      else if (evt.key === "Enter") { evt.preventDefault(); doConfirm(); }
    }

    confirmBtn.onclick = doConfirm;
    cancelBtn.onclick = doCancel;
    overlay.onclick = (evt) => { if (evt.target === overlay) doCancel(); };
    document.addEventListener("keydown", onKeydown);

    overlay.hidden = false;
    setTimeout(() => {
      const toFocus = isText ? inputEl : isTextarea ? textareaEl : confirmBtn;
      toFocus.focus();
      if (isText) inputEl.select();
    }, 0);
  });
}

function showAlert(message, opts = {}) {
  return openModal({ mode: "alert", title: opts.title || "Notice", message });
}
function showConfirm(message, opts = {}) {
  return openModal({
    mode: "confirm",
    title: opts.title || "Confirm",
    message,
    danger: opts.danger,
    confirmLabel: opts.confirmLabel,
    cancelLabel: opts.cancelLabel,
  });
}
function showTextPrompt(message, opts = {}) {
  return openModal({
    mode: "text",
    title: opts.title || "Enter value",
    message,
    defaultValue: opts.defaultValue,
    placeholder: opts.placeholder,
  });
}
function showTextareaPrompt(message, opts = {}) {
  return openModal({
    mode: "textarea",
    title: opts.title || "Enter value",
    message,
    defaultValue: opts.defaultValue,
    placeholder: opts.placeholder,
    confirmLabel: opts.confirmLabel,
    attachments: opts.attachments,
  });
}

// A real dropdown, not a popup dialog: a small floating box (no dark
// overlay) positioned near opts.anchor ({x, y} in viewport coords),
// holding a native <select> that resolves the moment an option is
// picked - no separate OK button needed. Escape or a click outside it
// cancels (resolves null). Used for interface-picking during Add Link,
// which used to be a full modal (a <select> for GNS3 nodes with real
// live ports, or a blind free-text field for ones without).
// Positions a fixed-position floating element near a viewport-space
// anchor point ({x, y}, e.g. a click's clientX/clientY), clamped so it
// never runs off-screen. Shared by every floating popover in this
// app - the interface-picker dropdown, the node-actions popup.
function positionNear(el, anchor) {
  const a = anchor || { x: window.innerWidth / 2, y: window.innerHeight / 2 };
  const rect = el.getBoundingClientRect();
  const left = Math.max(8, Math.min(a.x + 12, window.innerWidth - rect.width - 12));
  const top = Math.max(8, Math.min(a.y + 12, window.innerHeight - rect.height - 12));
  el.style.left = `${left}px`;
  el.style.top = `${top}px`;
}

function showInlineDropdown(options, opts = {}) {
  return new Promise((resolve) => {
    const pop = document.createElement("div");
    pop.className = "inline-dropdown";

    if (opts.title) {
      const label = document.createElement("div");
      label.className = "inline-dropdown-title";
      label.textContent = opts.title;
      pop.appendChild(label);
    }

    const select = document.createElement("select");
    select.className = "inline-dropdown-select";
    for (const opt of options) {
      const o = document.createElement("option");
      o.value = opt.value;
      o.textContent = opt.label;
      if (opt.disabled) o.disabled = true;
      select.appendChild(o);
    }
    if (opts.defaultValue !== undefined) select.value = opts.defaultValue;
    pop.appendChild(select);

    const hint = document.createElement("div");
    hint.className = "inline-dropdown-hint";
    hint.textContent = "Enter to accept, Esc to cancel";
    pop.appendChild(hint);

    document.body.appendChild(pop);
    positionNear(pop, opts.anchor);

    function finish(result) {
      document.removeEventListener("mousedown", onOutside, true);
      document.removeEventListener("keydown", onKeydown, true);
      pop.remove();
      resolve(result);
    }
    function onOutside(evt) {
      if (!pop.contains(evt.target)) finish(null);
    }
    function onKeydown(evt) {
      if (evt.key === "Escape") { evt.preventDefault(); finish(null); }
      // Accept whatever's currently selected (the default, if the
      // operator never touched the dropdown at all) without forcing
      // them to actually change the selection first.
      else if (evt.key === "Enter") { evt.preventDefault(); finish(select.value); }
    }
    select.addEventListener("change", () => finish(select.value));
    // Deferred a tick - the same click that opened this dropdown (a
    // node click in link mode) is still bubbling as a mousedown right
    // now, and would otherwise immediately look like an "outside
    // click" and close what it just opened.
    setTimeout(() => document.addEventListener("mousedown", onOutside, true), 0);
    document.addEventListener("keydown", onKeydown, true);
    select.focus();
  });
}

const NODE_W = 70;
