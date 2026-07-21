# meerail — convenience targets.
#
# Primary deploy is the Dockerized server; the agent runs natively next to
# Proton Bridge. `make up` runs the whole app; `make agent` runs the connector.

COMPOSE ?= docker compose

.PHONY: help up down logs build infra dev venv agent desktop psql fmt

help:
	@echo "meerail targets:"
	@echo "  make up      - build + run the full server stack (server + postgres + tika)"
	@echo "  make down    - stop the stack"
	@echo "  make logs    - tail server logs"
	@echo "  make infra   - run only postgres + tika (for native server dev)"
	@echo "  make dev     - run the server natively with --reload (needs 'make infra' + venv)"
	@echo "  make venv    - create .venv and install server deps"
	@echo "  make agent   - run the meerail-agent (see agent/README.md)"
	@echo "  make desktop - run the native Electron app (needs the server running)"
	@echo "  make psql    - open a psql shell on the bundled Postgres"

up:
	$(COMPOSE) up --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f server

# Only the backing services, so you can run the server natively with reload.
infra:
	$(COMPOSE) up -d db tika

venv:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt

dev:
	DATABASE_URL=$${DATABASE_URL:-postgresql+psycopg://meerail:meerail@localhost:5432/meerail} \
	TIKA_URL=$${TIKA_URL:-http://localhost:9998} \
	.venv/bin/uvicorn app.main:app --reload --port 8000

agent:
	cd agent && ./run.sh

desktop:
	cd electron && npm install && npm start

psql:
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-meerail} -d $${POSTGRES_DB:-meerail}
