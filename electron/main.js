/* meerail desktop shell.
 *
 * A thin Electron wrapper around the local meerail server: it loads the web app
 * in a native window, opens outbound links in the system browser, and shows a
 * retry screen if the server isn't running.
 *
 * Point it at a non-default server with MEERAIL_URL:
 *   MEERAIL_URL=http://localhost:8000 npm start
 *
 * Accept a certificate nothing on this machine trusts, for that server only:
 *   MEERAIL_TRUST_CERT=/path/to/certs/cert.pem npm start
 *
 * Override the spellchecker languages with MEERAIL_SPELLCHECK_LANGS:
 *   MEERAIL_SPELLCHECK_LANGS=en-GB,fr npm start
 */
const { app, BrowserWindow, clipboard, shell, Menu, MenuItem } = require("electron");
const fs = require("fs");
const path = require("path");

/* Where the server is. Not a constant, because of the upgrade in
 * upgradeToHttps() below: a server with `password` set speaks TLS and nothing
 * else on its port, and following it there has to take the origin the window
 * checks its links against along with it.
 */
let appUrl = (process.env.MEERAIL_URL || "http://localhost:8000").replace(/\/+$/, "");
let appOrigin = new URL(appUrl).origin;

/* One certificate to accept for that origin whatever this machine thinks of it.
 *
 * Chromium gives the desktop app no way past a certificate warning: the load
 * fails where a browser would have asked, so a self-signed certificate — the
 * one docker-compose.tls.yml writes into ./certs — locks the app out entirely.
 * The other way in is the machine's trust store, which is a different chore on
 * each platform and trusts the certificate for the whole machine. This trusts
 * exactly the certificate in this file, and only for the server the app was
 * pointed at. Give it an absolute path: a packaged app does not start in the
 * directory you launched it from.
 */
const PINNED_CERT = readPinnedCert(process.env.MEERAIL_TRUST_CERT);

function readPinnedCert(file) {
  if (!file) return null;
  try {
    const der = derOf(fs.readFileSync(file, "utf8"));
    if (!der) throw new Error("no PEM certificate in it");
    return der;
  } catch (err) {
    console.warn(`MEERAIL_TRUST_CERT: ignoring ${file} — ${err.message}`);
    return null;
  }
}

// The base64 body of a PEM certificate, armour and line breaks taken out, so
// that two spellings of the same certificate compare equal.
function derOf(pem) {
  const m = /-----BEGIN CERTIFICATE-----([\s\S]*?)-----END CERTIFICATE-----/.exec(pem || "");
  return m ? m[1].replace(/\s+/g, "") : null;
}

function originOf(url) {
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

// Chromium checks every language in this list at once, so a mail written in
// German and one written in English are both checked without a manual switch.
const SPELLCHECK_LANGS = (process.env.MEERAIL_SPELLCHECK_LANGS || "en-US,de-DE")
  .split(",").map((l) => l.trim()).filter(Boolean);

let mainWindow = null;

function isInternal(targetUrl) {
  return originOf(targetUrl) === appOrigin;
}

/* Chromium closes the door on an untrusted certificate; PINNED_CERT is the one
 * key. Compared by the certificate itself rather than by its fingerprint or its
 * name, so nothing but the file the user named gets in — and only for the
 * server they pointed the app at, never for a link that led somewhere else.
 */
app.on("certificate-error", (event, _webContents, url, _error, certificate, callback) => {
  const trusted = Boolean(PINNED_CERT)
    && originOf(url) === appOrigin
    && derOf(certificate.data) === PINNED_CERT;
  if (trusted) event.preventDefault();
  callback(trusted);
});

/* A server with `password` set does not answer over plaintext at all (README:
 * "HTTPS without a proxy in front"), and the app's default URL is the plaintext
 * one — so turning the password on made the desktop app report a server that was
 * not running, or leave a blank window. Issue #15. Both ways out are the same
 * move: take the same address over TLS, and take the origin the window checks
 * its links against along with it, or every click in the app would leave for
 * the system browser.
 *
 * Upward only, and once.
 */
// How a TLS socket turns a plaintext request away: uvicorn's answers to one are
// ERR_EMPTY_RESPONSE and ERR_CONNECTION_RESET, and the others are the same
// hangup told slightly differently. A server that is simply not there says
// ERR_CONNECTION_REFUSED and is not in this list, which is the point of it.
const TLS_SHAPED = new Set([
  "ERR_EMPTY_RESPONSE", "ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED",
  "ERR_INVALID_HTTP_RESPONSE",
]);
let upgraded = false;

function httpsForm() {
  return `https://${appUrl.slice("http://".length)}`;
}

function adoptHttps() {
  appUrl = httpsForm();
  appOrigin = new URL(appUrl).origin;
  upgraded = true;
  return appUrl;
}

/* In this stack there is no plaintext listener to redirect anyone: uvicorn holds
 * the port with TLS, and an http:// request to it is answered by the connection
 * being dropped. That is not "the server is down", however much it looks like it
 * from here, so try TLS before saying so.
 */
function upgradeToHttps(errorDescription) {
  if (upgraded || !appUrl.startsWith("http://") || !TLS_SHAPED.has(errorDescription)) return false;
  console.log(`${errorDescription} over plaintext — retrying at ${httpsForm()}`);
  mainWindow.loadURL(adoptHttps());
  return true;
}

/* Where TLS is terminated somewhere in front of the server, there *is* a
 * plaintext listener, and it answers with a redirect to https:// on the same
 * host. That redirect is the app moving, not a link leading off it — so follow
 * it in the same sense: adopt the origin Chromium is already on its way to.
 */
function followHttpsRedirect(url) {
  if (upgraded || !appUrl.startsWith("http://")) return;
  if (originOf(url) === originOf(httpsForm())) adoptHttps();
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 720,
    minHeight: 520,
    backgroundColor: "#ffffff",
    title: "meerail",
    icon: path.join(__dirname, "build", "icon.png"),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      partition: "persist:meerail",
      spellcheck: true,
    },
  });

  setUpSpellCheck(mainWindow);

  // The window's session is persistent, so its HTTP cache outlives a restart:
  // an asset cached before the server started sending Cache-Control stays put
  // until its heuristic freshness runs out, and a shell running half-old js
  // against a half-new server fails in confusing ways. The server is local, so
  // refetching the asset set on launch costs nothing worth keeping.
  const win = mainWindow;
  win.webContents.session.clearCache()
    .finally(() => { if (!win.isDestroyed()) win.loadURL(appUrl); });

  // Links to other origins open in the system browser, never a child window.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (e, url) => {
    if (!isInternal(url)) {
      e.preventDefault();
      shell.openExternal(url);
    }
  });
  mainWindow.webContents.on("will-redirect", (_e, url) => followHttpsRedirect(url));

  // If the page won't load, show a screen that says why instead of a blank
  // window. -3 is ERR_ABORTED, which is what a navigation replaced by another
  // one reports; it is not a failure anyone needs to see.
  mainWindow.webContents.on("did-fail-load", (_e, errorCode, desc, validatedURL) => {
    if (errorCode === -3 || !isInternal(validatedURL || appUrl)) return;
    if (upgradeToHttps(desc)) return;
    mainWindow.loadURL(errorPage(desc));
  });

  trackForeground(mainWindow);

  mainWindow.on("closed", () => { mainWindow = null; });
}

/* Tell the page when the window stops being the one in front, so it can stand
 * its polling, its event stream and its animations down (see app.power.js).
 *
 * The page can already see `visibilitychange`, but that only fires for a window
 * that is minimised or hidden. A window sitting fully visible behind another is
 * still not the app the user is in — that is the ordinary case on a desktop and
 * the one that costs, because a mail client left open behind a browser polls and
 * animates all day. Only the shell knows about it, so the shell says so.
 *
 * Sent with executeJavaScript rather than IPC because this window has no preload
 * script: adding one to carry a single boolean would be a larger change to the
 * security surface than the message is worth.
 */
function signalForeground(win, state) {
  if (!win || win.isDestroyed()) return;
  win.webContents
    .executeJavaScript(`window.dispatchEvent(new Event("meerail:${state}"))`)
    // The page may be mid-navigation, or be the error screen, which has no
    // listener and does not need one. Never worth failing a window event over.
    .catch(() => {});
}

function trackForeground(win) {
  win.on("focus", () => signalForeground(win, "focus"));
  win.on("blur", () => signalForeground(win, "blur"));
  // Minimise and hide raise `visibilitychange` in the page by themselves on most
  // platforms, but not all, and a restore does not reliably raise `focus`.
  // Saying it a second time is free: App.power ignores a resume it is not
  // suspended for, and re-suspending an already-suspended app is a no-op.
  win.on("show", () => signalForeground(win, "focus"));
  win.on("restore", () => signalForeground(win, "focus"));
  win.on("hide", () => signalForeground(win, "blur"));
  win.on("minimize", () => signalForeground(win, "blur"));
  // A reload takes the page's listeners with it, and the shell's events do not
  // fire again just because the document changed. Re-state the current answer,
  // or a window reloaded while in the background would come back polling.
  win.webContents.on("did-finish-load",
    () => signalForeground(win, win.isFocused() ? "focus" : "blur"));
}

/* "Open Link" / "Copy Link Address" for the context menu below.
 *
 * A mailto: link is an address, not a URL, so it copies as one — pasting
 * `mailto:x@y?subject=hi` into a To: field is never what was wanted. Its query
 * string goes with the scheme; what is left is percent-decoded, since that is
 * how the address was typed. */
function appendLinkItems(menu, url) {
  const mail = /^mailto:/i.test(url);
  let copy = url;
  if (mail) {
    const addr = url.slice(7).split("?")[0];
    try { copy = decodeURIComponent(addr); } catch { copy = addr; }
  }
  menu.append(new MenuItem({
    label: mail ? "New Message to Address" : "Open Link in Browser",
    click: () => shell.openExternal(url),
  }));
  menu.append(new MenuItem({
    label: mail ? "Copy Email Address" : "Copy Link Address",
    click: () => clipboard.writeText(copy),
  }));
  menu.append(new MenuItem({ type: "separator" }));
}

/* Spell checking for the compose fields.
 *
 * Chromium fetches the Hunspell dictionary for each language on first use and
 * caches it under the user data dir, so the first run needs network access; a
 * language whose dictionary hasn't arrived yet simply isn't checked. macOS uses
 * the OS spellchecker instead, which manages its own languages -- setting the
 * list there is a no-op, so we skip it.
 *
 * Chromium marks misspellings but leaves the correction UI to the app, hence
 * the context menu below.
 */
function setUpSpellCheck(win) {
  const session = win.webContents.session;

  if (process.platform !== "darwin") {
    const available = session.availableSpellCheckerLanguages;
    const langs = SPELLCHECK_LANGS.filter((l) => available.includes(l));
    const unknown = SPELLCHECK_LANGS.filter((l) => !available.includes(l));
    if (unknown.length) {
      console.warn(`spellcheck: ignoring unsupported language(s) ${unknown.join(", ")}`);
    }
    if (langs.length) session.setSpellCheckerLanguages(langs);
  }

  win.webContents.on("context-menu", (_e, params) => {
    const menu = new Menu();

    // Right-clicking a link in a message: copying the address is the whole
    // point of the gesture, and this menu replacing Chromium's own is what
    // took it away. Comes first, and works the same in a mail body's iframe.
    if (params.linkURL) appendLinkItems(menu, params.linkURL);

    for (const suggestion of params.dictionarySuggestions) {
      menu.append(new MenuItem({
        label: suggestion,
        click: () => win.webContents.replaceMisspelling(suggestion),
      }));
    }
    if (params.misspelledWord) {
      if (params.dictionarySuggestions.length) menu.append(new MenuItem({ type: "separator" }));
      menu.append(new MenuItem({
        label: "Add to Dictionary",
        click: () => session.addWordToSpellCheckerDictionary(params.misspelledWord),
      }));
      menu.append(new MenuItem({ type: "separator" }));
    }

    // Without a menu of our own the default one is gone, so keep the basics.
    menu.append(new MenuItem({ role: "cut", enabled: params.editFlags.canCut }));
    menu.append(new MenuItem({ role: "copy", enabled: params.editFlags.canCopy }));
    menu.append(new MenuItem({ role: "paste", enabled: params.editFlags.canPaste }));
    menu.append(new MenuItem({ type: "separator" }));
    menu.append(new MenuItem({ role: "selectAll" }));

    menu.popup({ window: win });
  });
}

/* The screen the window falls back to, saying which failure this was.
 *
 * A certificate the machine does not trust used to land here as "can't reach
 * the server", which sent people looking at a server that was running fine and
 * answering. It is the one failure the shell cannot offer a way past, so it is
 * the one that has to explain itself.
 */
function errorPage(errorDescription) {
  const cert = /^ERR_CERT/.test(errorDescription || "");
  const heading = cert
    ? "meerail's certificate isn't trusted"
    : "Can't reach the meerail server";
  let body = `<p>Couldn't connect to <code>${appOrigin}</code>. Start it with
       <code>docker compose up -d</code> and try again.</p>`;
  if (cert) {
    body = `<p>The server at <code>${appOrigin}</code> is answering, but it presented a
       certificate nothing on this machine trusts (<code>${esc(errorDescription)}</code>), and the
       desktop app has no "proceed anyway" the way a browser does.</p>
       <p>Point it at that certificate with
       <code>MEERAIL_TRUST_CERT=/path/to/certs/cert.pem</code>, or trust the certificate on
       this machine — see <em>HTTPS without a proxy in front</em> in the README.</p>`;
  } else if (upgraded) {
    // The dead end issue #15 is about: a password turned the HTTPS requirement
    // on, and nothing was put on the other side of it.
    body = `<p>meerail requires HTTPS here, and nothing answered at
       <code>${appOrigin}</code> (<code>${esc(errorDescription)}</code>).</p>
       <p>That requirement comes from <code>password</code> being set, which means something has
       to be serving TLS. <code>docker-compose.tls.yml</code> in the repo root is the smallest
       way to arrange it — see <em>HTTPS without a proxy in front</em> in the README.</p>`;
  }
  const html = `<!DOCTYPE html><html><head><meta charset="utf-8" />
    <style>
      body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
        font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f5f5f7;color:#1d1d1f}
      .card{text-align:center;max-width:440px;padding:2rem}
      h1{font-size:1.3rem;margin:0 0 .5rem}p{color:#57606a;line-height:1.5}
      button{font:inherit;font-weight:600;cursor:pointer;border:none;border-radius:8px;
        padding:.7rem 1.3rem;background:#1d6ff2;color:#fff;margin-top:1rem}
      code{background:#e6e8eb;padding:.15rem .4rem;border-radius:5px}
    </style></head><body><div class="card">
      <h1>${heading}</h1>
      ${body}
      <button onclick="location.href='${appUrl}'">Retry</button>
    </div></body></html>`;
  return "data:text/html;charset=utf-8," + encodeURIComponent(html);
}

// Chromium's own wording, and it never contains markup — but it is put into a
// page, and text that goes into a page gets escaped.
function esc(text) {
  return String(text || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

function buildMenu() {
  const isMac = process.platform === "darwin";
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    ...(isMac ? [{ role: "appMenu" }] : []),
    { role: "fileMenu" },
    { role: "editMenu" },
    {
      label: "View",
      submenu: [
        { label: "Home", accelerator: "CmdOrCtrl+Shift+H",
          click: () => mainWindow && mainWindow.loadURL(appUrl) },
        { role: "reload" }, { role: "forceReload" }, { type: "separator" },
        { role: "resetZoom" }, { role: "zoomIn" }, { role: "zoomOut" }, { type: "separator" },
        { role: "togglefullscreen" }, { role: "toggleDevTools" },
      ],
    },
    { role: "windowMenu" },
  ]));
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(() => {
    buildMenu();
    createWindow();
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });
}

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
