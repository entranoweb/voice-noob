.PHONY: help setup install dev stop clean test test-db test-backend test-frontend test-caller lint format migrate check check-backend check-frontend preflight

# The backend suite runs against Postgres, as CI does. With no test database
# set it falls back to SQLite, which fails tests that are correct (it drops
# time zones and cannot build array columns). Each test gets its own schema in
# this database and drops it afterwards, so it is safe to reuse.
#
# The password is the one `make setup` generated into the root .env for the
# compose database. Only that one line is read from the file.
POSTGRES_PASSWORD ?= $(shell sed -n 's/^POSTGRES_PASSWORD=//p' .env 2>/dev/null)
ifeq ($(strip $(POSTGRES_PASSWORD)),)
POSTGRES_PASSWORD := postgres
endif
TEST_DATABASE_URL ?= postgresql+asyncpg://postgres:$(POSTGRES_PASSWORD)@localhost:5432/voicenoob_test

help:
	@echo "Available commands:"
	@echo "  make setup          - First run: env files, dependencies, database, migrations"
	@echo "  make dev            - Start Postgres and Redis and print how to run the apps"
	@echo "  make stop           - Stop all services"
	@echo "  make clean          - Remove containers, volumes and installed dependencies"
	@echo "  make test           - Run backend (on Postgres), frontend and caller tests"
	@echo "  make test-backend   - Backend tests only"
	@echo "  make test-frontend  - Frontend tests only"
	@echo "  make test-caller    - Caller tests only"
	@echo "  make lint           - Run linters"
	@echo "  make format         - Format code"
	@echo "  make migrate        - Run database migrations"
	@echo "  make check          - Run all quality checks (lint + typecheck + format)"
	@echo "  make check-backend  - Run backend checks only"
	@echo "  make check-frontend - Run frontend checks only"
	@echo "  make preflight      - Check a deployment before placing a real call"

# Everything a clean machine needs, in the order it needs it. Safe to re-run:
# existing env files are kept and migrations only apply what is missing.
setup:
	uv run --no-project python scripts/dev_env.py
	cd backend && uv sync --all-extras
	cd caller && uv sync --all-extras
	cd frontend && npm install
	docker compose up -d --wait postgres redis
	cd backend && uv run alembic upgrade head
	@echo ""
	@echo "Ready. Run the two apps in separate terminals:"
	@echo "  cd backend && uv run uvicorn app.main:app --reload"
	@echo "  cd frontend && npm run dev"

install: setup

dev:
	@echo "Starting development environment..."
	docker compose up -d --wait postgres redis
	cd backend && uv run alembic upgrade head
	@echo "Services started. Run the following in separate terminals:"
	@echo "  Backend:  cd backend && uv run uvicorn app.main:app --reload"
	@echo "  Frontend: cd frontend && npm run dev"

stop:
	docker compose down

clean:
	docker compose down -v
	rm -rf backend/.venv
	rm -rf caller/.venv
	rm -rf frontend/node_modules
	rm -rf frontend/.next

test: test-backend test-frontend test-caller

# Starts Postgres and creates the test database when it is missing.
test-db:
	docker compose up -d --wait postgres
	docker compose exec -T postgres sh -c "psql -U postgres -tAc \"SELECT 1 FROM pg_database WHERE datname='voicenoob_test'\" | grep -q 1 || createdb -U postgres voicenoob_test"

test-backend: test-db
	cd backend && TEST_DATABASE_URL=$(TEST_DATABASE_URL) uv run pytest

test-frontend:
	cd frontend && npm test

test-caller:
	cd caller && uv run pytest

lint:
	cd backend && uv run ruff check .
	cd backend && uv run mypy app
	cd frontend && npm run lint

format:
	cd backend && uv run ruff format .
	cd frontend && npm run lint:fix

migrate:
	cd backend && uv run alembic upgrade head

check:
	@echo "Running all quality checks..."
	bash scripts/check-all.sh

check-backend:
	@echo "Running backend checks..."
	cd backend && bash scripts/check.sh

check-frontend:
	@echo "Running frontend checks..."
	cd frontend && npm run check

preflight:
	cd backend && uv run python scripts/preflight_telnyx.py $(ARGS)
