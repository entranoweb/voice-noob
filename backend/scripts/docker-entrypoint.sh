#!/bin/sh
set -e

echo "🚀 Starting Synthiq Voice Platform Backend..."

# Wait for PostgreSQL to be ready (simple retry loop)
echo "⏳ Waiting for PostgreSQL..."
until pg_isready -h "${POSTGRES_HOST:-postgres}" -p "${POSTGRES_PORT:-5432}" -U "${POSTGRES_USER:-postgres}" -q 2>/dev/null; do
    echo "PostgreSQL not ready, waiting..."
    sleep 2
done
echo "✅ PostgreSQL is ready"

# Wait for Redis to be ready
echo "⏳ Waiting for Redis..."
# REDISCLI_AUTH, not -a: the password stays out of the process list.
until REDISCLI_AUTH="${REDIS_PASSWORD:-}" redis-cli -h "${REDIS_HOST:-redis}" ping 2>/dev/null | grep -q PONG; do
    echo "Redis not ready, waiting..."
    sleep 2
done
echo "✅ Redis is ready"

# Run database migrations
echo "📦 Running database migrations..."
alembic upgrade head
echo "✅ Migrations complete"

# Start the application
echo "🎯 Starting Gunicorn server..."
exec gunicorn app.main:app -c gunicorn.conf.py
