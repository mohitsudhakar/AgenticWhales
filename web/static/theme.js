// Shared theme controller for every AgenticWhales surface.
//
// Loaded NON-deferred in <head> so it sets data-theme before first paint
// (no flash of the wrong theme). Exposes window.AWTheme and auto-wires any
// element carrying [data-theme-toggle]:
//
//   <button data-theme-toggle>
//     <span data-theme-icon>☾</span><span data-theme-label>Dark</span>
//   </button>
(function () {
  var KEY = "aw_theme";

  function get() {
    try { return localStorage.getItem(KEY) === "light" ? "light" : "dark"; }
    catch (_) { return "dark"; }
  }

  function apply(t) {
    document.documentElement.setAttribute("data-theme", t === "light" ? "light" : "dark");
  }

  // Run immediately (head, pre-paint).
  apply(get());

  function syncBtn(btn) {
    var light = get() === "light";
    btn.setAttribute("aria-pressed", String(light));
    btn.setAttribute("title", light ? "Switch to dark" : "Switch to light");
    var icon = btn.querySelector("[data-theme-icon]");
    var label = btn.querySelector("[data-theme-label]");
    if (icon) icon.textContent = light ? "☀" : "☾";
    if (label) label.textContent = light ? "Light" : "Dark";
  }

  function set(t) {
    try { localStorage.setItem(KEY, t); } catch (_) {}
    apply(t);
    document.querySelectorAll("[data-theme-toggle]").forEach(syncBtn);
    window.dispatchEvent(new CustomEvent("aw-theme-change", { detail: { theme: get() } }));
  }

  function toggle() { set(get() === "light" ? "dark" : "light"); }

  window.AWTheme = { get: get, set: set, toggle: toggle };

  window.addEventListener("DOMContentLoaded", function () {
    var existing = document.querySelectorAll("[data-theme-toggle]");
    // Pages that don't ship their own toggle (analyze / usage / landing) get a
    // floating one for free.
    if (existing.length === 0) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "theme-toggle floating";
      b.setAttribute("data-theme-toggle", "");
      b.innerHTML = '<span data-theme-icon aria-hidden="true">☾</span><span data-theme-label>Dark</span>';
      document.body.appendChild(b);
      existing = [b];
    }
    existing.forEach(function (btn) {
      syncBtn(btn);
      btn.addEventListener("click", toggle);
    });
  });
})();
