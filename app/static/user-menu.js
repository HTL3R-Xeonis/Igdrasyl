// [user-menu.js] Shows who is logged in (GET /me) and an Admin Panel
// link when that user is an admin. Logout just POSTs /logout and
// reloads - the server redirects the next page load to /login.
(async () => {
  try {
    const resp = await fetch("/me");
    if (!resp.ok) return;
    const { username, role } = await resp.json();
    document.getElementById("user-menu-name").textContent = `${username} (${role})`;
    document.getElementById("user-menu-admin-link").hidden = role !== "admin";
  } catch {
    // Not fatal - the menu just stays blank; every other request already enforces login.
  }
})();

document.getElementById("user-menu-logout-btn").addEventListener("click", async () => {
  await fetch("/logout", { method: "POST" });
  window.location.href = "/login";
});
