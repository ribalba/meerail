# meerail — convenience targets.
#
# Primary deploy is the Dockerized server; the agent runs natively next to
# Proton Bridge. `make up` runs the whole app; `make agent` runs the connector.

COMPOSE ?= docker compose

# The base stack keeps Postgres and Tika on the internal `meerail` network with
# no published ports — only the server is reachable from the host. Anything that
# talks to them from *outside* Docker (a native agent, `make dev`) needs the
# hostports overlay, which republishes both on 127.0.0.1.
HOST_FILES = -f docker-compose.yml -f docker-compose.hostports.yml

# The agent overlay needs the host network namespace, so it is Linux-only and
# lives in its own file rather than the base one. Host networking puts it
# outside the compose network too, hence the hostports overlay as well. See
# agent/README.md.
AGENT_FILES = $(HOST_FILES) -f docker-compose.agent.yml

# The test stack is a separate compose project so it can never share a container,
# network or volume with production. Ports are shifted (55432 / 18000).
TEST_COMPOSE = $(COMPOSE) -p meerail-test -f docker-compose.test.yml
TEST_DATABASE_URL = postgresql+psycopg://meerail:meerail@127.0.0.1:55432/meerail_test
TEST_MEERAIL_URL = http://127.0.0.1:18000
TEST_TIKA_URL = http://127.0.0.1:59998
PYTEST ?= .venv-test/bin/pytest

.PHONY: help up up-hostports down logs build infra dev venv agent agent-docker agent-test agent-logs agent-service agent-service-status agent-service-stop desktop psql fmt test test-up test-down test-psql screenshots

help:
	@echo "meerail targets:"
	@echo "  make up      - build + run the full server stack (server + postgres + tika)"
	@echo "  make up-hostports - same, but also publish postgres/tika on 127.0.0.1 (native agent)"
	@echo "  make down    - stop the stack"
	@echo "  make logs    - tail server logs"
	@echo "  make infra   - run only postgres + tika (for native server dev)"
	@echo "  make dev     - run the server natively with --reload (needs 'make infra' + venv)"
	@echo "  make venv    - create .venv and install server deps"
	@echo "  make agent   - run the meerail-agent natively (see agent/README.md)"
	@echo "  make agent-docker - run the agent in Docker, host network (Linux only)"
	@echo "  make agent-test   - check the agent's connections in Docker, then exit"
	@echo "  make agent-logs   - tail agent logs"
	@echo "  make agent-service        - macOS: run the agent in the background at login"
	@echo "  make agent-service-status - macOS: is the background agent running?"
	@echo "  make agent-service-stop   - macOS: stop and remove the background agent"
	@echo "  make desktop - run the native Electron app (needs the server running)"
	@echo "  make psql    - open a psql shell on the bundled Postgres"
	@echo "  make test    - run the suite on a throwaway stack (never touches prod)"
	@echo "  make test-up   - bring up the test stack and leave it running"
	@echo "  make test-down - tear the test stack down, discarding its data"
	@echo "  make test-psql - psql shell on the test database"
	@echo "  make screenshots - reseed the demo mailbox and re-shoot the website images"

up:
	$(COMPOSE) up --build

# Same stack with Postgres and Tika also published on 127.0.0.1. Needed when the
# agent runs natively rather than in the compose network — which is always the
# case on macOS and Windows.
up-hostports:
	$(COMPOSE) $(HOST_FILES) up --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f server

# Only the backing services, so you can run the server natively with reload.
# Uses the hostports overlay because `make dev` reaches them over loopback.
infra:
	$(COMPOSE) $(HOST_FILES) up -d db tika

venv:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt

dev:
	DATABASE_URL=$${DATABASE_URL:-postgresql+psycopg://meerail:meerail@localhost:5432/meerail} \
	TIKA_URL=$${TIKA_URL:-http://localhost:9998} \
	.venv/bin/uvicorn app.main:app --reload --port 8000 --timeout-graceful-shutdown 3

agent:
	cd agent && ./run.sh

# Linux only — host networking is what lets the container see Bridge, Postgres
# and Tika on 127.0.0.1. On macOS/Windows run `make agent` instead.
#
# Brings up the whole stack, not just the agent: host networking only reaches
# Postgres and Tika through the loopback ports the overlay publishes, so db and
# tika have to be (re)created with those files in play.
agent-docker:
	$(COMPOSE) $(AGENT_FILES) up -d --build

agent-test:
	$(COMPOSE) $(AGENT_FILES) run --rm agent --test

agent-logs:
	$(COMPOSE) $(AGENT_FILES) logs -f agent

# macOS only — the launchd equivalent of what agent-docker does on Linux:
# start at login, restart on failure. `service.sh logs` tails it.
agent-service:
	cd agent && ./service.sh install

agent-service-status:
	cd agent && ./service.sh status

agent-service-stop:
	cd agent && ./service.sh uninstall

desktop:
	cd electron && npm install && npm start

psql:
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-meerail} -d $${POSTGRES_DB:-meerail}

# --- tests -------------------------------------------------------------------
#
# `down -v` FIRST, not just after: it discards the Postgres volume so the stack
# comes up on a freshly initdb'd cluster every time. Doing it only on the way out
# would leave a dirty database behind if a run were interrupted.
test-up:
	$(TEST_COMPOSE) down -v --remove-orphans
	$(TEST_COMPOSE) up -d --build --wait

test-down:
	$(TEST_COMPOSE) down -v --remove-orphans

test-psql:
	$(TEST_COMPOSE) exec db psql -U meerail -d meerail_test

# Runs against the throwaway stack, then tears it down whichever way pytest went
# (the `;` + exit keeps the failure code instead of masking it with teardown's).
test: test-up
	@DATABASE_URL="$(TEST_DATABASE_URL)" \
	 MEERAIL_URL="$(TEST_MEERAIL_URL)" \
	 TIKA_URL="$(TEST_TIKA_URL)" \
	 $(PYTEST) tests/ $(PYTEST_ARGS); \
	 status=$$?; \
	 $(TEST_COMPOSE) down -v --remove-orphans; \
	 exit $$status

# --- website screenshots -----------------------------------------------------
#
# Runs on the same throwaway stack as the suite, for the same reason: the seed
# truncates every table before it writes, so it must never see production. The
# stack is left up afterwards rather than torn down — a failed shot is usually
# easier to diagnose by opening $(TEST_MEERAIL_URL) and looking.
#
# Uses .venv-test, which already carries playwright, Pillow and PyMuPDF. If the
# browser is missing: .venv-test/bin/playwright install chromium
SHOOT_ENV = DATABASE_URL="$(TEST_DATABASE_URL)" \
            MEERAIL_URL="$(TEST_MEERAIL_URL)" \
            TIKA_URL="$(TEST_TIKA_URL)"

screenshots: test-up
	@$(SHOOT_ENV) .venv-test/bin/python website/screenshots/seed.py
	@$(SHOOT_ENV) .venv-test/bin/python website/screenshots/shoot.py $(SHOOT_ARGS)
	@echo "stack still up — 'make test-down' when finished"
