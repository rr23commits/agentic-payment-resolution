PROJECT_ROOT := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
ENV_FILE := $(PROJECT_ROOT).env

ifneq (,$(wildcard $(ENV_FILE)))
include $(ENV_FILE)
export
endif

.PHONY: dev test db-up db-down migrate seed

dev: db-up migrate seed
	uv run python -m backend.main

test: db-up migrate
	uv run python -m unittest discover -s tests

db-up:
	docker compose up -d --wait postgres

db-down:
	docker compose down

migrate:
	uv run python -m backend.migrate

seed:
	uv run python -m backend.seed
