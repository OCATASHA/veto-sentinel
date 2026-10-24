// Apply the saved theme and expert-mode choices before first paint (no flash).
(function () {
  try {
    var t = localStorage.getItem("vs-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
    if (localStorage.getItem("vs-expert") === "1") document.documentElement.classList.add("expert");
  } catch (e) { /* storage unavailable: defaults apply */ }
})();
