// Runs before the page paints so the saved (or system) theme never flashes. Kept tiny on purpose.
(function () {
  var t = null;
  try { t = localStorage.getItem("rig-theme"); } catch (e) { /* storage blocked: follow the system */ }
  if (t !== "light" && t !== "dark") t = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  document.documentElement.dataset.theme = t;
})();
