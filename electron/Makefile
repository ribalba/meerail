# meerail desktop (Electron) build & run helpers.
#
# The app points at the local server via the MEERAIL_URL env var (see main.js).
# Override it on the command line if needed, e.g.  make start MEERAIL_URL=http://meerail.local:8000

MEERAIL_URL ?= http://localhost:8000
export MEERAIL_URL

NPM ?= npm

# Where `distinstall` puts things. Everything lands under the user's XDG dirs,
# so no root is needed. Override e.g. with  make distinstall XDG_DATA_HOME=/opt/share
XDG_DATA_HOME ?= $(HOME)/.local/share
APP_HOME      := $(XDG_DATA_HOME)/meerail
APP_IMAGE     := $(APP_HOME)/meerail.AppImage
BIN_DIR       := $(HOME)/.local/bin
DESKTOP_DIR   := $(XDG_DATA_HOME)/applications
DESKTOP_FILE  := $(DESKTOP_DIR)/meerail.desktop
ICON_DIR      := $(XDG_DATA_HOME)/icons/hicolor/512x512/apps

.PHONY: help install start pack dist clean distinstall distuninstall

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

distinstall: dist ## Build the AppImage and register it with the desktop (KDE/GNOME)
	@set -e; \
	src=$$(ls -t dist/*.AppImage 2>/dev/null | head -n 1); \
	if [ -z "$$src" ]; then echo "No AppImage in dist/ — is 'linux' a build target?" >&2; exit 1; fi; \
	echo "Installing $$src -> $(APP_IMAGE)"; \
	mkdir -p "$(APP_HOME)" "$(BIN_DIR)" "$(DESKTOP_DIR)" "$(ICON_DIR)"; \
	install -m 0755 "$$src" "$(APP_IMAGE)"; \
	install -m 0644 build/icon.png "$(ICON_DIR)/meerail.png"; \
	ln -sfn "$(APP_IMAGE)" "$(BIN_DIR)/meerail"; \
	printf '%s\n' \
		'[Desktop Entry]' \
		'Type=Application' \
		'Name=meerail' \
		'GenericName=Email Client' \
		'Comment=Desktop wrapper for the meerail email client.' \
		'Exec=env MEERAIL_URL=$(MEERAIL_URL) $(APP_IMAGE) %U' \
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
	for k in kbuildsycoca6 kbuildsycoca5; do command -v $$k >/dev/null 2>&1 && $$k >/dev/null 2>&1 && break; done; true; \
	echo "Installed. Launch from your app menu, or run 'meerail' (needs $(BIN_DIR) on PATH)."

distuninstall: ## Remove the installed AppImage, launcher and icon
	@rm -f "$(DESKTOP_FILE)" "$(ICON_DIR)/meerail.png" "$(BIN_DIR)/meerail" "$(APP_IMAGE)"
	@rmdir "$(APP_HOME)" 2>/dev/null || true
	@command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$(DESKTOP_DIR)" || true
	@command -v gtk-update-icon-cache  >/dev/null 2>&1 && gtk-update-icon-cache -f -t "$(XDG_DATA_HOME)/icons/hicolor" >/dev/null 2>&1 || true
	@for k in kbuildsycoca6 kbuildsycoca5; do command -v $$k >/dev/null 2>&1 && $$k >/dev/null 2>&1 && break; done; true
	@echo "Uninstalled."

clean: ## Remove build output
	rm -rf dist
