# meerail desktop

A thin Electron wrapper that opens the meerail web app in a native window. It
needs the **server running** (`docker compose up -d` in the repo root) and, on
the same machine or another, the **agent** connected to Proton Bridge.

## Run in development

```bash
cd electron
npm install
npm start                        # loads http://localhost:8000
MEERAIL_URL=http://host:8000 npm start   # or a remote server
```

## A server that speaks HTTPS

An install with `server.password` set serves TLS and turns plaintext away, which
is [`docker-compose.tls.yml`](../docker-compose.tls.yml) in the repo root. The URL
needs no change: a plaintext request to that port is answered by the connection
being dropped rather than by a redirect, and the shell retries the same address
over `https://` by itself.

The certificate does. Chromium gives the app no way past a certificate warning —
the load fails where a browser would have asked — so a self-signed certificate has
to be arranged in advance. Either hand the app that one certificate:

```bash
MEERAIL_TRUST_CERT=/absolute/path/to/certs/cert.pem npm start
```

which is accepted for that server and nothing else, or put it in the machine's
trust store, where the thing to get right is that it is a server certificate and
not a CA — `certutil -d sql:$HOME/.pki/nssdb -A -t "P,," -n meerail -i
certs/cert.pem` on Linux, *Always Trust* against SSL in the macOS Keychain, the
Windows "Trusted People" store. The README's [HTTPS without a proxy in
front](../README.md#https-without-a-proxy-in-front) has the longer version.

## Build installers

```bash
npm run dist        # -> dist/  (macOS .dmg/.zip, Linux .AppImage/.deb, Windows .exe)
```

## Install on Linux (KDE / GNOME)

```bash
make distinstall    # build, then register with the desktop
make distuninstall  # remove it again
```

`distinstall` builds the AppImage and installs it for the current user (no root):

| what | where |
| --- | --- |
| AppImage | `~/.local/share/meerail/meerail.AppImage` |
| CLI symlink | `~/.local/bin/meerail` |
| launcher | `~/.local/share/applications/meerail.desktop` |
| icon | `~/.local/share/icons/hicolor/512x512/apps/meerail.png` |

It then refreshes the desktop/icon caches (`update-desktop-database`,
`gtk-update-icon-cache`, `kbuildsycoca6`), so the app shows up in the KDE and
GNOME menus right away. The launcher pins the server URL, so pass it in if it
isn't the default: `make distinstall MEERAIL_URL=http://meerail.local:8000`.
`StartupWMClass=meerail` keeps the window grouped with the launcher in the task
bar/dock. `MEERAIL_TRUST_CERT=...` goes into the same line when it is set, and is
left out of it when it is not.

`electron-builder` targets are configured in `package.json`. The app icon is
`build/icon.png`, a 1024×1024 export (the size macOS prefers as an `icns`
source). Code signing/notarization is not configured — add your certs for
distributable macOS builds.

## Warnings during `npm install`

The install prints deprecation warnings for `inflight`, `glob@7`, `rimraf@2`
and `boolean`. All four come from `electron-builder`'s own dependency tree
(`@electron/asar`, `electron-winstaller`, `@electron/get`) — nothing this
project depends on directly, and nothing that ends up in the shipped app,
which bundles only `main.js`, `package.json` and the icon. Newer releases of
those packages are ESM-only and Node ≥ 22.12, which `electron-builder` cannot
consume yet, so an `overrides` block would break the build rather than fix the
warnings. They go away when `electron-builder` updates upstream.

`electron` on its own installs 13 packages with no warnings; the noise is
entirely the packaging toolchain. `npm audit` should report no
vulnerabilities — if it does not, `npm audit fix --package-lock-only` and
commit the lockfile.
