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
bar/dock.

`electron-builder` targets are configured in `package.json`. The app icon is
`build/icon.png` (replace with a higher-resolution export for production; macOS
prefers a 1024×1024 source). Code signing/notarization is not configured — add
your certs for distributable macOS builds.
