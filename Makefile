# Developer tasks for VolunteerDB. Run `make` for the list.
#
# These are thin wrappers over the commands in the documentation — nothing is
# hidden and nothing is required: every target maps to something you can still
# type by hand. Override the tooling if your setup differs, e.g.
#   make db COMPOSE=docker-compose

.DEFAULT_GOAL := help
SHELL := /bin/bash

COMPOSE ?= podman compose
UV      ?= uv
DB_USER := volunteerdb

# Extra arguments for `make test`, e.g. make test ARGS="-k roster -x"
ARGS ?=

.PHONY: help db down clean migrate seed dev serve test lint format docs fresh

help: ## list these targets
	@echo "VolunteerDB developer tasks"
	@echo
	@grep -hE '^[a-z][a-z-]*:.*?## ' $(MAKEFILE_LIST) | sort \
	  | awk -F':.*?## ' '{printf "  \033[1m%-8s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "First time here: make seed && make dev"

# No prerequisites on purpose: make builds this only when .env is missing, so
# it can never clobber an .env you have already edited.
.env:
	@cp .env.example .env
	@secret=$$(python3 -c 'import secrets; print(secrets.token_hex(32))'); \
	  sed -i "s|^VDB_STORAGE_SECRET=.*|VDB_STORAGE_SECRET=$$secret|" .env
	@echo "==> created .env with a generated VDB_STORAGE_SECRET"

db: .env ## start PostgreSQL and wait until it accepts queries
	@$(COMPOSE) up -d db
	@printf '==> waiting for postgres'
	@for i in {1..60}; do \
	  if $(COMPOSE) exec -T db pg_isready -U $(DB_USER) -q >/dev/null 2>&1; then \
	    echo ' ready'; exit 0; \
	  fi; \
	  printf '.'; sleep 1; \
	done; \
	echo ' timed out after 60s' >&2; exit 1

migrate: db ## create or update the schema (alembic upgrade head)
	$(UV) run alembic upgrade head

seed: migrate ## load the demo parish and print its logins
	$(UV) run python scripts/seed.py

# Must be `python -m`, not the `volunteerdb` console script: reload respawns a
# worker that re-imports the module, and only the `__mp_main__` guard in
# main.py calls ui.run() there. The console script imports main as
# `volunteerdb.main`, the guard never fires, and startup dies with
# "You must call ui.run() to start the server".
dev: db ## serve http://localhost:8080, restarting on source changes
	VDB_RELOAD=true $(UV) run python -m volunteerdb.main

serve: db ## serve http://localhost:8080 without auto-reload
	$(UV) run volunteerdb

test: db ## run the test suite (scratch volunteerdb_test database)
	$(UV) run pytest $(ARGS)

lint: ## check the source with ruff (no writes)
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format: ## reformat and sort imports in place (ruff)
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

docs: ## build the HTML manual into docs/_build/html
	$(UV) run --group docs sphinx-build -W -b html docs docs/_build/html

fresh: ## wipe the database volume, then migrate and seed from scratch
	@$(MAKE) clean
	@$(MAKE) seed

down: ## stop the containers, keeping the data volume
	$(COMPOSE) down

clean: ## stop the containers AND delete the database volume
	$(COMPOSE) down -v
