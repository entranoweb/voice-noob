# Backend Dockerfile for Synthiq Voice Platform
FROM python:3.12-slim AS builder

WORKDIR /app

# Install uv for fast dependency management
RUN pip install uv

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-dev

# Production stage
FROM python:3.12-slim

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    postgresql-client \
    redis-tools \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application code
COPY . .

# Make entrypoint executable
RUN chmod +x /app/scripts/docker-entrypoint.sh

# Run as an unprivileged user. The server binds port 8000, writes its logs to
# stdout and needs nothing a root process has; a flaw in the application or a
# dependency should not hand out root inside the container.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --no-create-home app \
    && chown -R app:app /app
USER app

# Set environment variables
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Expose port
EXPOSE 8000

# Use entrypoint script for migrations
ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
