"""Create local env files for development. Run by `make setup`.

`backend/.env.example` cannot be used as it stands: the app refuses to start on
the placeholder SECRET_KEY, the admin password is a published string, and
PUBLIC_URL points at a production host. This writes `backend/.env` from the
example with those three replaced, and copies `frontend/.env.example`.

An env file that already exists is left alone, so running this twice is safe
and never rotates a key under a running database.

Standard library only: it runs before any dependency is installed.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LOCAL_OVERRIDES = {
    "PUBLIC_URL": "http://localhost:8000",
    # A placeholder here would make the app report the key as configured.
    "ANTHROPIC_API_KEY": "",
}


def render_backend_env(example: str, secret_key: str, admin_password: str) -> str:
    """Return the example with the values that must not be shared replaced."""
    replacements = {
        "SECRET_KEY": secret_key,
        "ADMIN_PASSWORD": admin_password,
        **LOCAL_OVERRIDES,
    }
    seen: set[str] = set()
    lines = []
    for line in example.splitlines():
        name = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and name in replacements:
            lines.append(f"{name}={replacements[name]}")
            seen.add(name)
        else:
            lines.append(line)
    missing = sorted(set(replacements) - seen)
    if missing:
        # The example changed shape; say so instead of writing a half-set file.
        raise SystemExit(f"backend/.env.example has no line for: {', '.join(missing)}")
    return "\n".join(lines) + "\n"


def main() -> int:
    backend_env = ROOT / "backend" / ".env"
    if backend_env.exists():
        print(f"kept    {backend_env.relative_to(ROOT)} (already exists, not changed)")
    else:
        example = (ROOT / "backend" / ".env.example").read_text(encoding="utf-8")
        admin_password = secrets.token_urlsafe(18)
        backend_env.write_text(
            render_backend_env(example, secrets.token_hex(32), admin_password),
            encoding="utf-8",
            newline="\n",
        )
        print(f"created {backend_env.relative_to(ROOT)}")
        print("        first sign-in: admin@example.com")
        print(f"        password:      {admin_password}")
        print("        (also in backend/.env as ADMIN_PASSWORD; used once, on an empty database)")

    frontend_env = ROOT / "frontend" / ".env"
    if frontend_env.exists():
        print(f"kept    {frontend_env.relative_to(ROOT)} (already exists, not changed)")
    else:
        example = (ROOT / "frontend" / ".env.example").read_text(encoding="utf-8")
        frontend_env.write_text(example, encoding="utf-8", newline="\n")
        print(f"created {frontend_env.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
