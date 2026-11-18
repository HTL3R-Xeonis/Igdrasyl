// [user-menu.js] Account actions in the upper-right corner of the canvas.
const userMenu = document.getElementById("user-menu");
const userMenuName = document.getElementById("user-menu-name");
const userMenuActions = document.getElementById("user-menu-actions");
const userMenuLogout = document.getElementById("user-menu-logout-btn");

function setUserMenuOpen(open) {
  userMenuActions.hidden = !open;
  userMenuName.setAttribute("aria-expanded", String(open));
}

userMenu.addEventListener("pointerenter", (event) => {
  if (event.pointerType === "mouse") setUserMenuOpen(true);
});
userMenu.addEventListener("pointerleave", () => {
  if (!userMenuActions.contains(document.activeElement)) setUserMenuOpen(false);
});
userMenuName.addEventListener("click", (event) => {
  setUserMenuOpen(event.pointerType === "mouse" || userMenuActions.hidden);
});
userMenu.addEventListener("focusout", (event) => {
  if (!userMenu.contains(event.relatedTarget) && !userMenu.matches(":hover")) {
    setUserMenuOpen(false);
  }
});
userMenu.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || userMenuActions.hidden) return;
  event.stopPropagation();
  userMenuName.focus();
  setUserMenuOpen(false);
});
document.addEventListener("click", (event) => {
  if (!userMenu.contains(event.target)) setUserMenuOpen(false);
});

(async () => {
  try {
    const resp = await fetch("/me");
    if (!resp.ok) return;
    const { username, role } = await resp.json();
    userMenuName.textContent = username;
    userMenuName.title = username;
    document.getElementById("user-menu-admin-link").hidden = role !== "admin";
    userMenu.hidden = false;
    window.currentUser = { username, role };
    document.dispatchEvent(new CustomEvent("currentuser"));
  } catch {
    // Not fatal - the menu just stays blank; every other request already enforces login.
  }
})();

userMenuLogout.addEventListener("click", async () => {
  userMenuLogout.disabled = true;
  try {
    const response = await fetch("/logout", { method: "POST" });
    if (!response.ok) throw new Error("Logout failed. Please try again.");
    window.location.href = "/login";
  } catch {
    setMenubarMessage("Logout failed. Please try again.", { error: true });
  } finally {
    userMenuLogout.disabled = false;
  }
});
