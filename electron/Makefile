# meerail desktop (Electron) build & run helpers.
#
# The app points at the local server via the MEERAIL_URL env var (see main.js).
# Override it on the command line if needed, e.g.  make start MEERAIL_URL=http://meerail.local:8000

MEERAIL_URL ?= http://localhost:8000
export MEERAIL_URL

NPM ?= npm

.PHONY: help install start pack dist clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies
	$(NPM) install

start: ## Run the app against $(MEERAIL_URL) (needs the server up)
	$(NPM) start

pack: ## Build an unpacked app into dist/ (no installer)
	$(NPM) run pack

dist: ## Build distributable installers into dist/
	$(NPM) run dist

clean: ## Remove build output
	rm -rf dist
