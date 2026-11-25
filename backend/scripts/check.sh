#!/bin/bash
set -e

echo "🔍 Running comprehensive backend checks..."
echo ""

echo "📝 Running Ruff linter..."
uv run ruff check app tests
echo "✓ Ruff linting passed"
echo ""

echo "✨ Checking code formatting..."
uv run ruff format --check app tests
echo "✓ Code formatting check passed"
echo ""

echo "🔬 Running mypy type checking..."
uv run mypy app
echo "✓ Type checking passed"
echo ""

echo "🧪 Running tests..."
# On Postgres, as CI does: the SQLite fallback fails tests that are correct.
# `make test-db` (from the repository root) starts it and creates this database.
# The password is the one `make setup` generated into the root .env.
PGPW="$(sed -n 's/^POSTGRES_PASSWORD=//p' ../.env 2>/dev/null)"
export TEST_DATABASE_URL="${TEST_DATABASE_URL:-postgresql+asyncpg://postgres:${PGPW:-postgres}@localhost:5432/voicenoob_test}"
uv run pytest
echo "✓ Tests passed"
echo ""

echo "✅ All backend checks passed successfully!"
