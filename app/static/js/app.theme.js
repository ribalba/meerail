/* meerail theme — light, dark, or whatever the system says.

   Loaded from <head>, ahead of every other module: the attribute has to be on
   <html> before the first paint, or a forced-light window on a dark Mac comes
   up dark for a frame. Nothing here touches the rest of the app, so running
   this early costs nothing but the file. */

window.App = window.App || {};

App.theme = (() => {
  const KEY = "meerail.theme";
  const MODES = ["system", "light", "dark"];

  // Anything unrecognised — a stale value, a hand-edited key — reads as
  // "system", the setting's own default.
  function mode() {
    const saved = localStorage.getItem(KEY);
    return MODES.includes(saved) ? saved : "system";
  }

  // The CSS carries both palettes and picks between them with color-scheme;
  // all this does is say which. No attribute means "let the OS decide".
  function apply(m) {
    if (m === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", m);
  }

  function set(m) {
    if (!MODES.includes(m)) m = "system";
    localStorage.setItem(KEY, m);
    apply(m);
  }

  apply(mode());

  return { mode, set, MODES };
})();
