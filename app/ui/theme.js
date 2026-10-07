/* Applies the saved theme before the page paints. "auto" (no attribute) follows the system. */
(function () {
  "use strict";
  var mode = "auto";
  try { mode = window.localStorage.getItem("fn-theme") || "auto"; } catch (_) { /* storage blocked: follow the system */ }
  if (mode === "light" || mode === "dark") document.documentElement.setAttribute("data-theme", mode);
})();
