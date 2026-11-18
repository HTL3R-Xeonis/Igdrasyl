// [info-popover.js] "i" info icon + popover for help text that would otherwise sit permanently on screen.
//
// Markup (static HTML or makeInfoButton below): a .info-btn immediately followed by its .info-popover, both inside one
// .info-row. Behavior, all delegated so any number of pairs needs no JS of its own:
//   hover or keyboard focus on the icon opens the popover; leaving it closes it again, unless it was pinned;
//   a click pins it open (a second click closes it) - on touch screens the click is the whole interaction;
//   Esc or a click anywhere else closes every open popover; the pointer may move onto the popover (it stays open).
// Never use it inside #landing-view: the landing page has no hover effects and no tooltips (tests/test_info_text_guard.py).

let infoPopoverCounter = 0;

// A ready-made ".info-row" fragment (icon + popover) for text built in JS. Append it right after the label it explains,
// never inside a <label> that wraps an input (a click would activate the field).
function makeInfoButton(text) {
  const row = document.createElement("span");
  row.className = "info-row inline";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "info-btn";
  button.setAttribute("aria-label", "More information");
  button.textContent = "i";
  const popover = document.createElement("span");
  popover.className = "info-popover";
  popover.hidden = true;
  popover.textContent = text;
  row.append(button, popover);
  return row;
}

function infoPopoverOf(button) {
  const popover = button.nextElementSibling;
  return popover && popover.classList.contains("info-popover") ? popover : null;
}

function setInfoOpen(button, open, pinned = false) {
  const popover = infoPopoverOf(button);
  if (!popover) return;
  if (!popover.id) popover.id = `info-popover-${++infoPopoverCounter}`;
  popover.setAttribute("role", "note");
  button.setAttribute("aria-controls", popover.id);
  button.setAttribute("aria-expanded", String(open));
  popover.hidden = !open;
  if (open && pinned) button.dataset.pinned = "1";
  if (!open) delete button.dataset.pinned;
}

function closeAllInfo(except = null) {
  document.querySelectorAll(".info-btn[aria-expanded='true']").forEach((button) => {
    if (button !== except) setInfoOpen(button, false);
  });
}

function stillInsideRow(button, related) {
  const row = button.closest(".info-row");
  return Boolean(row && related && row.contains(related));
}

document.addEventListener("mouseover", (evt) => {
  const button = evt.target.closest(".info-btn");
  if (button && button.getAttribute("aria-expanded") !== "true") {
    closeAllInfo(button);
    setInfoOpen(button, true);
  }
});

document.addEventListener("mouseout", (evt) => {
  const row = evt.target.closest(".info-row");
  const button = row && row.querySelector(".info-btn");
  if (!button || button.dataset.pinned || stillInsideRow(button, evt.relatedTarget)) return;
  setInfoOpen(button, false);
});

document.addEventListener("focusin", (evt) => {
  const button = evt.target.closest(".info-btn");
  if (button) {
    closeAllInfo(button);
    setInfoOpen(button, true);
  }
});

document.addEventListener("focusout", (evt) => {
  const button = evt.target.closest(".info-btn");
  if (button && !button.dataset.pinned && !stillInsideRow(button, evt.relatedTarget)) setInfoOpen(button, false);
});

document.addEventListener("click", (evt) => {
  const button = evt.target.closest(".info-btn");
  if (!button) {
    if (!evt.target.closest(".info-popover")) closeAllInfo();
    return;
  }
  closeAllInfo(button);
  if (button.dataset.pinned) setInfoOpen(button, false);
  else setInfoOpen(button, true, true);
});

document.addEventListener("keydown", (evt) => {
  if (evt.key === "Escape") closeAllInfo();
});
