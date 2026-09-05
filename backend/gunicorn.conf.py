"""Gunicorn configuration for production deployment.

Run with: gunicorn -c gunicorn.conf.py app.main:app
"""

import multiprocessing
import os

# Server socket
bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8000")
backlog = 2048

# Worker processes
# Rule of thumb: (2 x $num_cores) + 1 for I/O bound apps
# For voice agents, we use fewer workers since each handles async I/O well
workers = int(os.getenv("GUNICORN_WORKERS", min(multiprocessing.cpu_count() * 2 + 1, 8)))
worker_class = "uvicorn.workers.UvicornWorker"
worker_connections = 1000
max_requests = 10000  # Restart workers after N requests (prevents memory leaks)
max_requests_jitter = 1000  # Add randomness to prevent all workers restarting together

# Timeout configuration
timeout = 120  # Longer timeout for voice WebSocket connections
graceful_timeout = 30  # Time to finish requests on shutdown
keepalive = 5  # Keep connections alive for reuse

# Process naming
proc_name = "synthiq-voice-api"

# Logging
accesslog = "-"  # stdout
errorlog = "-"  # stderr
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# Server mechanics
daemon = False
pidfile = None
umask = 0
user = None
group = None
tmp_upload_dir = None

# SSL configuration: set via environment variables GUNICORN_KEYFILE and GUNICORN_CERTFILE
keyfile = os.getenv("GUNICORN_KEYFILE")
certfile = os.getenv("GUNICORN_CERTFILE")


# Hooks for monitoring
def on_starting(server):
    """Called just before the master process is initialized."""


def on_reload(server):
    """Called to recycle workers during a reload via SIGHUP."""


def when_ready(server):
    """Called just after the server is started."""


def pre_fork(server, worker):
    """Called just before a worker is forked."""


def post_fork(server, worker):
    """Called just after a worker has been forked."""


def post_worker_init(worker):
    """Called just after a worker has initialized the application."""


def worker_int(worker):
    """Called when a worker receives SIGINT or SIGQUIT."""


def worker_abort(worker):
    """Called when a worker receives SIGABRT."""


def pre_exec(server):
    """Called just before a new master process is forked."""


def child_exit(server, worker):
    """Called when a worker exits."""


def worker_exit(server, worker):
    """Called just after a worker has been exited."""


def nworkers_changed(server, new_value, old_value):
    """Called when the number of workers changes."""
