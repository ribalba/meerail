/* meerail update notice — "a newer version is out".

   The server does the actual checking (app/updates.py); this only renders the
   verdict. That split is deliberate: the browser never talks to github, so one
   install makes one request a day no matter how many tabs are open, and turning
   the check off in meerail.toml genuinely turns it off rather than just hiding
   the banner.

   Deliberately quiet. It is a strip in the sidebar, not a modal — nothing here
   is urgent, and an update prompt that interrupts you reading your mail earns
   itself a permanent dismissal. Dismissing pins the *version* that was
   dismissed, so the next release says its piece and this one stays quiet. */

App.update = (() => {
  const $ = (s) => document.querySelector(s);

  // The version the user has already waved away. Per browser, like every other
  // local preference; a value of "0.4.0" means "stop telling me about 0.4.0".
  const KEY = "meerail.update.dismissed";

  // The page stays open for days at a time, so a check on boot alone would
  // never fire on the machine most likely to fall behind. The server caches for
  // a day regardless — this is how often we ask it what it knows, not how often
  // it asks github.
  const RECHECK = 6 * 3600 * 1000;

  let info = null;

  function dismissed() {
    try { return localStorage.getItem(KEY) || ""; } catch { return ""; }
  }

  function dismiss(version) {
    try { localStorage.setItem(KEY, version); } catch { /* private mode */ }
    render();
  }

  function render() {
    const box = $("#update-notice");
    if (!box) return;

    const show = !!info && info.update_available && dismissed() !== info.latest;
    box.hidden = !show;
    if (!show) {
      box.innerHTML = "";
      return;
    }

    // The link goes to the README's "How to update", not the releases page:
    // whoever clicks this wants the command for their install, not a changelog.
    box.innerHTML =
      `<a class="un-text" href="${App.esc(info.update_url)}" target="_blank" rel="noopener noreferrer"
          title="meerail ${App.esc(info.latest)} is available — you are running ${App.esc(info.version)}. How to update.">` +
        `<span class="un-icon">${App.icon("download", 13)}</span>` +
        `<span class="un-label">Update available — ${App.esc(info.latest)}</span>` +
      `</a>` +
      `<button class="un-close" type="button" title="Dismiss until the next release"
               aria-label="Dismiss">${App.icon("close", 12)}</button>`;

    box.querySelector(".un-close").addEventListener("click", () => dismiss(info.latest));
  }

  // The Settings modal's About block. Filled whether or not an update is out —
  // it is the answer to "which version am I running", which is the first
  // question of every bug report.
  function renderAbout() {
    const line = $("#about-version");
    if (!line) return;
    if (!info) { line.textContent = "—"; return; }

    let text = `meerail ${info.version}`;
    if (!info.check_enabled) text += " — update checks are off";
    else if (info.update_available) text += ` — ${info.latest} is available`;
    else if (info.latest) text += " — up to date";
    // No `latest` with checks on means the first check has not come back yet
    // (or could not): say nothing rather than claim to be current.
    line.textContent = text;
  }

  async function refresh() {
    try {
      info = await App.api.get("/api/version");
    } catch {
      // Offline, or a server too old to have the endpoint. Neither is worth a
      // word on screen — App.conn already owns "the server is unreachable".
      return;
    }
    render();
    renderAbout();
  }

  function init() {
    // Not awaited: the boot path should not wait on this, and there is nothing
    // to show until it comes back anyway.
    refresh();
    setInterval(refresh, RECHECK);
  }

  return { init, refresh, current: () => info };
})();
