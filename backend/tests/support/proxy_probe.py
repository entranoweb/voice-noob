"""A tiny app served by the real gunicorn config in tests/integration/test_proxy_headers.py.

It uses the application's own rate limiter and the audit module's own address
function, so what the test observes is what the real routes would see. The
limiter counts in memory: the test runs one worker.
"""

from fastapi import FastAPI, Request
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.core.audit import client_ip
from app.core.limiter import build_limiter

limiter = build_limiter("memory://")
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]


@app.get("/probe")
@limiter.limit("3/minute")
async def probe(request: Request) -> dict[str, str | None]:
    return {"limiter_key": get_remote_address(request), "audit_ip": client_ip(request)}
