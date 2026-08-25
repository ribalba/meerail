# meerail desktop (Electron) build & run helpers.
#
# The app points at the local server via the MEERAIL_URL env var (see main.js).
# Override it on the command line if needed, e.g.  make start MEERAIL_URL=http://meerail.local:8000

MEERAIL_URL ?= http://localhost:8000
export MEERAIL_URL

# A certificate to accept for that server even though this machine has no reason
# to — ./certs/cert.pem, if the server is running docker-compose.tls.yml. Empty
# for the ordinary install, where there is no TLS to vouch for. An absolute path:
# an installed app does not start in the directory you typed make in.
#   make start MEERAIL_TRUST_CERT=$PWD/../certs/cert.pem
MEERAIL_TRUST_CERT ?=
export MEERAIL_TRUST_CERT

# Written into the launcher only when it is set, so the ordinary install has
# nothing extra in its Exec line.
TRUST_ENV := $(if $(MEERAIL_TRUST_CERT),MEERAIL_TRUST_CERT=$(MEERAIL_TRUST_CERT) ,)

NPM ?= npm

# Where `distinstall` puts things. Everything lands under the user's own dirs,
# so no root is needed. Override e.g. with  make distinstall XDG_DATA_HOME=/opt/share
BIN_DIR       := $(HOME)/.local/bin

# Linux: an AppImage plus an XDG desktop entry.
XDG_DATA_HOME ?= $(HOME)/.local/share
APP_HOME      := $(XDG_DATA_HOME)/meerail
APP_IMAGE     := $(APP_HOME)/meerail.AppImage
DESKTOP_DIR   := $(XDG_DATA_HOME)/applications
DESKTOP_FILE  := $(DESKTOP_DIR)/meerail.desktop
# hicolor tops out at 512x512, and an icon theme only looks in the directory
# whose name matches the file's real size. build/icon.png is 1024 because that
# is what macOS wants as an icns source, so it gets scaled down on the way in.
ICON_DIR      := $(XDG_DATA_HOME)/icons/hicolor/512x512/apps

# macOS: the .app bundle itself is the install. ~/Applications rather than
# /Applications keeps it to this user and needs no sudo; Spotlight, Launchpad
# and the Dock all index it just the same.
MAC_APP_DIR   ?= $(HOME)/Applications
MAC_APP       := $(MAC_APP_DIR)/meerail.app

# `distinstall` means something different per desktop, so it is one name in
# front of two recipes rather than a Linux-only target that fails everywhere
# else with a puzzling "no AppImage in dist/".
ifeq ($(shell uname -s),Darwin)
INSTALL_KIND := macos
else
INSTALL_KIND := linux
endif

.PHONY: help install start pack dist clean distinstall distuninstall \
        distinstall-linux distuninstall-linux distinstall-macos distuninstall-macos

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies
	$(NPM) install

start: ## Run the app against $(MEERAIL_URL) (needs the server up)
	$(NPM) start

pack: ## Build an unpacked app into dist/ (no installer)
	$(NPM) run pack

dist: ## Build distributable installers into dist/
	$(NPM) run dist

distinstall: distinstall-$(INSTALL_KIND) ## Build and install the app for this desktop (macOS or Linux)

distuninstall: distuninstall-$(INSTALL_KIND) ## Remove what distinstall put in place

distinstall-linux: dist
	@set -e; \
	src=$$(ls -t dist/*.AppImage 2>/dev/null | head -n 1); \
	if [ -z "$$src" ]; then echo "No AppImage in dist/ — is 'linux' a build target?" >&2; exit 1; fi; \
	echo "Installing $$src -> $(APP_IMAGE)"; \
	mkdir -p "$(APP_HOME)" "$(BIN_DIR)" "$(DESKTOP_DIR)" "$(ICON_DIR)"; \
	install -m 0755 "$$src" "$(APP_IMAGE)"; \
	if command -v magick >/dev/null 2>&1; then magick build/icon.png -resize 512x512 "$(ICON_DIR)/meerail.png"; \
	elif command -v convert >/dev/null 2>&1; then convert build/icon.png -resize 512x512 "$(ICON_DIR)/meerail.png"; \
	else install -m 0644 build/icon.png "$(ICON_DIR)/meerail.png"; fi; \
	chmod 0644 "$(ICON_DIR)/meerail.png"; \
	ln -sfn "$(APP_IMAGE)" "$(BIN_DIR)/meerail"; \
	printf '%s\n' \
		'[Desktop Entry]' \
		'Type=Application' \
		'Name=meerail' \
		'GenericName=Email Client' \
		'Comment=Desktop wrapper for the meerail email client.' \
		'Exec=env MEERAIL_URL=$(MEERAIL_URL) $(TRUST_ENV)$(APP_IMAGE) %U' \
		'TryExec=$(APP_IMAGE)' \
		'Icon=meerail' \
		'Terminal=false' \
		'Categories=Network;Email;' \
		'Keywords=email;mail;meerail;' \
		'StartupNotify=true' \
		'StartupWMClass=meerail' \
		> "$(DESKTOP_FILE)"; \
	chmod 0644 "$(DESKTOP_FILE)"; \
	command -v desktop-file-validate >/dev/null 2>&1 && desktop-file-validate "$(DESKTOP_FILE)" || true; \
	command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$(DESKTOP_DIR)" || true; \
	command -v gtk-update-icon-cache  >/dev/null 2>&1 && gtk-update-icon-cache -f -t "$(XDG_DATA_HOME)/icons/hicolor" >/dev/null 2>&1 || true; \
	rm -f "$(HOME)/.cache/icon-cache.kcache"; \
	for k in kbuildsycoca6 kbuildsycoca5; do command -v $$k >/dev/null 2>&1 && $$k >/dev/null 2>&1 && break; done; true; \
	echo "Installed. Launch from your app menu, or run 'meerail' (needs $(BIN_DIR) on PATH)."

distuninstall-linux:
	@rm -f "$(DESKTOP_FILE)" "$(ICON_DIR)/meerail.png" "$(BIN_DIR)/meerail" "$(APP_IMAGE)"
	@rmdir "$(APP_HOME)" 2>/dev/null || true
	@command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$(DESKTOP_DIR)" || true
	@command -v gtk-update-icon-cache  >/dev/null 2>&1 && gtk-update-icon-cache -f -t "$(XDG_DATA_HOME)/icons/hicolor" >/dev/null 2>&1 || true
	@rm -f "$(HOME)/.cache/icon-cache.kcache"
	@for k in kbuildsycoca6 kbuildsycoca5; do command -v $$k >/dev/null 2>&1 && $$k >/dev/null 2>&1 && break; done; true
	@echo "Uninstalled."

# The .dmg is for handing to someone else; installing here means the bundle
# electron-builder already staged. Copied with ditto rather than cp, which is
# the one that carries a bundle's symlinks, extended attributes and code
# signature across intact — an arm64 build is ad-hoc signed and will not launch
# if the copy breaks that.
distinstall-macos: dist
	@set -e; \
	src=$$(ls -td dist/mac*/meerail.app 2>/dev/null | head -n 1); \
	if [ -z "$$src" ]; then echo "No meerail.app in dist/ — is 'mac' a build target?" >&2; exit 1; fi; \
	echo "Installing $$src -> $(MAC_APP)"; \
	mkdir -p "$(MAC_APP_DIR)" "$(BIN_DIR)"; \
	rm -rf "$(MAC_APP)"; \
	ditto "$$src" "$(MAC_APP)"; \
	ln -sfn "$(MAC_APP)/Contents/MacOS/meerail" "$(BIN_DIR)/meerail"; \
	echo "Installed. Launch from Spotlight or Launchpad, or run 'meerail' (needs $(BIN_DIR) on PATH)."; \
	if [ "$(MEERAIL_URL)" != "http://localhost:8000" ] || [ -n "$(MEERAIL_TRUST_CERT)" ]; then \
		echo; \
		echo "Note: this environment is not baked into the bundle — macOS does not give"; \
		echo "      a Finder launch your shell environment, so the app menu starts on the"; \
		echo "      default http://localhost:8000 and trusts no certificate of its own."; \
		echo "      Run 'meerail' from a shell that has MEERAIL_URL / MEERAIL_TRUST_CERT"; \
		echo "      set to reach $(MEERAIL_URL)."; \
	fi

distuninstall-macos:
	@rm -rf "$(MAC_APP)"
	@rm -f "$(BIN_DIR)/meerail"
	@rmdir "$(MAC_APP_DIR)" 2>/dev/null || true
	@echo "Uninstalled."

clean: ## Remove build output
	rm -rf dist
