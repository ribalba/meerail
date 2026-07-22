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

`electron-builder` targets are configured in `package.json`. The app icon is
`build/icon.png` (replace with a higher-resolution export for production; macOS
prefers a 1024×1024 source). Code signing/notarization is not configured — add
your certs for distributable macOS builds.
