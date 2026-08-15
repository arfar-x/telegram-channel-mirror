# Operational helpers for running this project under docker compose.
#
# Docker on the deploy host may require root (as in `sudo docker compose ...`) —
# override DC if yours doesn't, e.g.:
#   make DC="docker compose" update
DC ?= sudo docker compose

.DEFAULT_GOAL := help
.PHONY: help update pull build up up-all down restart logs logs-all ps \
        login shell db-shell backup-now prune lint

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

## --- Deploy -----------------------------------------------------------

update: pull build up ## Pull latest code, rebuild the mirror image, and recreate the container

pull: ## git pull the current branch
	git pull

build: ## Rebuild the mirror image (needed after any code change: the image bakes in the source, it isn't bind-mounted)
	$(DC) build mirror

up: ## Recreate the mirror container from the freshly built image (plain `docker compose up` alone would NOT do this)
	$(DC) up -d mirror

up-all: ## Bring up every service (mirror, postgres, backup)
	$(DC) up -d

down: ## Stop and remove all containers (does not touch the postgres/backup volumes)
	$(DC) down

restart: ## Restart the mirror container in place, without rebuilding (only useful if the code didn't change)
	$(DC) restart mirror

## --- Observability ------------------------------------------------------

logs: ## Follow mirror logs
	$(DC) logs -f --tail=200 mirror

logs-all: ## Follow logs from every service
	$(DC) logs -f --tail=200

ps: ## Show container status
	$(DC) ps

## --- Debugging ------------------------------------------------------------

login: ## First-time interactive Telegram login (Ctrl+C once past the OTP/2FA prompt, then `make up`)
	$(DC) run --rm -it mirror python main.py

shell: ## Open a shell inside the running mirror container
	$(DC) exec mirror /bin/bash

db-shell: ## Open a psql shell in the postgres container (uses its own POSTGRES_USER/POSTGRES_DB)
	$(DC) exec postgres sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

## --- Maintenance ------------------------------------------------------

backup-now: ## Manually trigger a database backup right now (normally runs on BACKUP_CRON_SCHEDULE)
	$(DC) exec backup /usr/local/bin/backup_postgres.sh

prune: ## Reclaim disk space by pruning dangling Docker images and build cache
	docker image prune -f
	docker builder prune -f

## --- Dev -----------------------------------------------------------------

lint: ## Run the same ruff check CI runs
	ruff check .
