// AgenticWhales is now a single elegant light theme — dark mode removed.
//
// Kept at the same path so the existing <script src="/static/theme.js"> tags
// on every page keep resolving (no 404s, no per-page HTML edits). It:
//   - forces data-theme="light" so any CSS still keyed on it behaves,
//   - clears the old persisted preference,
//   - removes any leftover [data-theme-toggle] controls in markup,
//   - leaves a harmless window.AWTheme shim so older callers don't throw.
(function () {
  try { localStorage.removeItem("aw_theme"); } catch (_) {}

  document.documentElement.setAttribute("data-theme", "light");

  // Back-compat shim: theme is fixed; toggling is a no-op.
  window.AWTheme = {
    get: function () { return "light"; },
    set: function () {},
    toggle: function () {},
  };

  function removeToggles() {
    document.querySelectorAll("[data-theme-toggle]").forEach(function (el) {
      el.remove();
    });
  }

  if (document.readyState === "loading") {
    window.addEventListener("DOMContentLoaded", removeToggles);
  } else {
    removeToggles();
  }
})();
