// [info-popover.js] Generic "?" info button + popover: click toggles the
// popover right after the clicked button (an .info-popover sibling),
// clicking anywhere else closes every open one. One delegated listener
// covers any number of .info-btn/.info-popover pairs added anywhere in
// the app, so a new one needs no JS of its own - just the markup.
document.addEventListener("click", (evt) => {
  const clicked = evt.target.closest(".info-btn");
  document.querySelectorAll(".info-popover").forEach((popover) => {
    popover.hidden = popover.previousElementSibling === clicked ? popover.hidden : true;
  });
  if (clicked) {
    const popover = clicked.nextElementSibling;
    if (popover && popover.classList.contains("info-popover")) {
      popover.hidden = !popover.hidden;
    }
  }
});
