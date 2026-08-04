# meerail — convenience targets.
#
# Primary deploy is the Dockerized server; the agent runs natively next to
# Proton Bridge. `make up` runs the whole app; `make agent` runs the connector.

COMPOSE ?= docker compose

# The agent overlay needs the host network namespace, so it is Linux-only and
# lives in its own file rather than the base one. See agent/README.md.
AGENT_FILES = -f docker-compose.yml -f docker-compose.agent.yml

# The test stack is a separate compose project so it can never share a container,
# network or volume with production. Ports are shifted (55432 / 18000).
TEST_COMPOSE = $(COMPOSE) -p meerail-test -f docker-compose.test.yml
TEST_DATABASE_URL = postgresql+psycopg://meerail:meerail@127.0.0.1:55432/meerail_test
TEST_MEERAIL_URL = http://127.0.0.1:18000
TEST_TIKA_URL = http://127.0.0.1:59998
PYTEST ?= .venv-test/bin/pytest

# --- release images ----------------------------------------------------------
#
# The three images `meerail.sh` pulls. VERSION is the single source of the
# number: it tags the images, stamps their OCI labels, and is what a running
# server compares itself against to notice an update (core/version.py).
DOCKER_ORG ?= ribalba
VERSION    := $(shell cat VERSION)
# Both architectures the project claims to support: Intel/AMD servers and
# Apple Silicon. Nothing here is compiled per-arch — the Python deps all ship
# aarch64 wheels — so the emulated half is not as slow as it sounds.
PLATFORMS  ?= linux/amd64,linux/arm64
# A named builder, because the default `docker` driver cannot do multi-platform
# builds at all. Created on demand by the buildx target below.
BUILDER    ?= meerail

.PHONY: help up down logs build infra dev venv backup restore agent agent-docker agent-test agent-logs agent-service agent-service-status agent-service-stop desktop psql fmt test test-up test-down test-psql screenshots version buildx images images-push

help:
	@echo "meerail targets:"
	@echo "  make up      - build + run the full server stack (server + postgres + tika)"
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
	@echo "  make backup    - dump the database to one compressed file under backups/"
	@echo "  make restore FILE=... - put one back, replacing the database"
	@echo "  make screenshots - reseed the demo mailbox and re-shoot the website images"
	@echo "  make version     - print the version everything is tagged with"
	@echo "  make images      - build the three release images for this machine only"
	@echo "  make images-push - build them for amd64+arm64 and push to Docker Hub"

up:
	$(COMPOSE) up --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f server

# Only the backing services, so you can run the server natively with reload.
# Both publish on 127.0.0.1, which is where `make dev` reaches them.
infra:
	$(COMPOSE) up -d db tika

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
# Postgres and Tika through their loopback ports, so they have to be running.
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
	 MEERAIL_CONFIG= \
	 $(PYTEST) tests/ $(PYTEST_ARGS); \
	 status=$$?; \
	 $(TEST_COMPOSE) down -v --remove-orphans; \
	 exit $$status

# --- backup and restore ------------------------------------------------------
#
# The clone's version of `meerail.sh backup` / `meerail.sh restore`; both write
# and read the same file, so a dump taken by one restores through the other.
#
# Compressed on the fly, not afterwards: pg_dump's custom format hands each
# block to zstd as it comes off the socket, so nothing uncompressed is ever
# written and the dump costs only the space it finally occupies. Level 19 with
# long-distance matching, because size was the thing to optimise — on a real
# mailbox that is ~3.6x smaller than the raw dump and within a percent of
# `xz -9`. meerail.sh carries the measurements and the reasoning; the short
# version is that `long` is free at any level and 19 is the level that then
# actually pays. BACKUP_COMPRESS trades it back for time:
#
#   make backup BACKUP_COMPRESS=zstd:level=12,long   # ~11% bigger, far quicker
#
# := so both recipe lines below see the same timestamp; a recursive ?= would
# re-run `date` on every expansion and write to a name it then cannot find.
BACKUP_DIR      ?= backups
BACKUP_COMPRESS ?= zstd:level=19,long
BACKUP_STAMP    := $(shell date +%Y%m%d-%H%M%S)
BACKUP_FILE     ?= $(BACKUP_DIR)/meerail-$(BACKUP_STAMP).dump

# Needs only `db` up — pg_dump takes its own snapshot, so the server and agent
# can go on writing while it runs. `.part` until it is whole, so an interrupted
# dump is never mistaken for a backup.
backup:
	@mkdir -p $(dir $(BACKUP_FILE))
	@$(COMPOSE) up -d --wait db
	@echo "dumping to $(BACKUP_FILE) — the stack can keep running"
	@$(COMPOSE) exec -T db pg_dump -U $${POSTGRES_USER:-meerail} -d $${POSTGRES_DB:-meerail} \
	  --format=custom --compress=$(BACKUP_COMPRESS) --no-owner --no-privileges \
	  > $(BACKUP_FILE).part || { rm -f $(BACKUP_FILE).part; exit 1; }
	@mv $(BACKUP_FILE).part $(BACKUP_FILE)
	@echo "wrote $(BACKUP_FILE) ($$(du -h $(BACKUP_FILE) | cut -f1))"

# Destructive, hence the typed confirmation. Stops the server first; a natively
# run agent is yours to stop — it will reconnect on its own afterwards, but
# anything it writes mid-restore is written into a database being replaced.
restore:
	@test -n "$(FILE)" || { echo "usage: make restore FILE=$(BACKUP_DIR)/meerail-....dump"; exit 1; }
	@test -f "$(FILE)" || { echo "no such file: $(FILE)"; exit 1; }
	@test "$$(head -c 5 $(FILE))" = "PGDMP" || { echo "$(FILE) is not a pg_dump archive"; exit 1; }
	@printf 'Replace the database with %s? Every message in it now is dropped. [type yes]: ' "$(FILE)"; \
	 read reply; [ "$$reply" = "yes" ] || { echo "nothing was changed"; exit 1; }
	@$(COMPOSE) stop server 2>/dev/null || true
	@$(COMPOSE) up -d --wait db
	@$(COMPOSE) exec -T db psql -U $${POSTGRES_USER:-meerail} -d postgres -q \
	  -c "DROP DATABASE IF EXISTS $${POSTGRES_DB:-meerail} WITH (FORCE)" \
	  -c "CREATE DATABASE $${POSTGRES_DB:-meerail} OWNER $${POSTGRES_USER:-meerail}"
	@echo "restoring — longer than the backup took, most of it rebuilding indexes"
	@$(COMPOSE) exec -T db pg_restore -U $${POSTGRES_USER:-meerail} -d $${POSTGRES_DB:-meerail} \
	  --no-owner --no-privileges --exit-on-error < $(FILE)
	@$(COMPOSE) up -d
	@echo "restored"

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
            TIKA_URL="$(TEST_TIKA_URL)" \
            MEERAIL_CONFIG=

# --- release images ----------------------------------------------------------
#
# CI does this on every push to main (.github/workflows/images.yml); these
# targets are the same commands by hand, for a one-off or a fork.
#
# `images` and `images-push` differ in more than the push: a multi-platform
# build cannot be loaded into the local image store (there is no such thing as
# a local multi-arch image without the containerd store), so a build you want
# to *run* is this machine's architecture only, and a build you want to
# *publish* goes straight from the builder to the registry. Hence two targets
# rather than one with a flag.

version:
	@echo $(VERSION)

# Idempotent: creates the builder the first time, selects it afterwards.
buildx:
	@docker buildx inspect $(BUILDER) >/dev/null 2>&1 \
	  || docker buildx create --name $(BUILDER) --driver docker-container --bootstrap
	@docker buildx use $(BUILDER)

# Native-architecture build, loaded locally — what you want before pushing
# anything, and what `docker compose -f docker-compose.hub.yml up` will find if
# you point MEERAIL_IMAGE_* at it.
images:
	docker build --build-arg MEERAIL_VERSION=$(VERSION) \
	  -t $(DOCKER_ORG)/meerail-server:$(VERSION) -t $(DOCKER_ORG)/meerail-server:latest .
	docker build --build-arg MEERAIL_VERSION=$(VERSION) -f agent/Dockerfile \
	  -t $(DOCKER_ORG)/meerail-agent:$(VERSION) -t $(DOCKER_ORG)/meerail-agent:latest .
	docker build --build-arg MEERAIL_VERSION=$(VERSION) \
	  -t $(DOCKER_ORG)/meerail-tika:$(VERSION) -t $(DOCKER_ORG)/meerail-tika:latest ./tika
	@echo
	@echo "Built $(DOCKER_ORG)/meerail-{server,agent,tika}:$(VERSION) for this machine."

# Publishes. Needs `docker login` first, and push rights on $(DOCKER_ORG).
# Every image gets both tags in one go, so :latest and :$(VERSION) can never
# point at different builds.
images-push: buildx
	docker buildx build --platform $(PLATFORMS) --build-arg MEERAIL_VERSION=$(VERSION) \
	  -t $(DOCKER_ORG)/meerail-server:$(VERSION) -t $(DOCKER_ORG)/meerail-server:latest --push .
	docker buildx build --platform $(PLATFORMS) --build-arg MEERAIL_VERSION=$(VERSION) \
	  -f agent/Dockerfile \
	  -t $(DOCKER_ORG)/meerail-agent:$(VERSION) -t $(DOCKER_ORG)/meerail-agent:latest --push .
	docker buildx build --platform $(PLATFORMS) --build-arg MEERAIL_VERSION=$(VERSION) \
	  -t $(DOCKER_ORG)/meerail-tika:$(VERSION) -t $(DOCKER_ORG)/meerail-tika:latest --push ./tika
	@echo
	@echo "Pushed $(DOCKER_ORG)/meerail-{server,agent,tika}:$(VERSION) (+ :latest) for $(PLATFORMS)."
	@echo "Installs notice the new version within a day; 'meerail.sh update' takes it now."

screenshots: test-up
	@$(SHOOT_ENV) .venv-test/bin/python website/screenshots/seed.py
	@$(SHOOT_ENV) .venv-test/bin/python website/screenshots/shoot.py $(SHOOT_ARGS)
	@echo "stack still up — 'make test-down' when finished"
