.PHONY: help install dev stop clean test lint format migrate check check-backend check-frontend preflight

help:
	@echo "Available commands:"
	@echo "  make install        - Install all dependencies"
	@echo "  make dev            - Start development environment"
	@echo "  make stop           - Stop all services"
	@echo "  make clean          - Clean up containers and volumes"
	@echo "  make test           - Run tests"
	@echo "  make lint           - Run linters"
	@echo "  make format         - Format code"
	@echo "  make migrate        - Run database migrations"
	@echo "  make check          - Run all quality checks (lint + typecheck + format)"
	@echo "  make check-backend  - Run backend checks only"
	@echo "  make check-frontend - Run frontend checks only"
	@echo "  make preflight      - Check a deployment before placing a real call"

install:
	@echo "Installing backend dependencies..."
	cd backend && uv sync --all-extras
	@echo "Installing frontend dependencies..."
	cd frontend && npm install
	@echo "Starting Docker services..."
	docker compose up -d postgres redis

dev:
	@echo "Starting development environment..."
	docker compose up -d postgres redis
	@echo "Services started. Run the following in separate terminals:"
	@echo "  Backend:  cd backend && uv run uvicorn app.main:app --reload"
	@echo "  Frontend: cd frontend && npm run dev"

stop:
	docker compose down

clean:
	docker compose down -v
	rm -rf backend/.venv
	rm -rf frontend/node_modules
	rm -rf frontend/.next

test:
	cd backend && uv run pytest
	cd frontend && npm test

lint:
	cd backend && uv run ruff check .
	cd backend && uv run mypy app
	cd frontend && npm run lint

format:
	cd backend && uv run ruff format .
	cd frontend && npm run lint --fix

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
