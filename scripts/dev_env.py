"""Create local env files for development. Run by `make setup`.

`backend/.env.example` cannot be used as it stands: the app refuses to start on
its placeholder SECRET_KEY and without a credential encryption key, the admin
and database passwords are published strings, and PUBLIC_URL points at a
production host. This writes:

- `backend/.env`, from the example, with every such value generated here;
- `.env` in the repository root, holding the generated database password for
  `docker-compose.yml`;
- `frontend/.env`, copied from its example.

No value written here is one the repository has shipped.

An env file that already exists is left alone, so running this twice is safe
and never rotates a key or a password under a running database.

Standard library only: it runs before any dependency is installed.
"""

from __future__ import annotations

import base64
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATABASE_URL_TEMPLATE = "postgresql+asyncpg://postgres:{password}@localhost:5432/voicenoob"


def new_credential_key_ring() -> str:
    """One key in the `<id>:<base64 32-byte key>` form the app parses."""
    return "k1:" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def render_backend_env(example: str, values: dict[str, str]) -> str:
    """Return the example with each named setting replaced by its value.

    Every name must have a line in the example: if the example changes shape,
    this fails loudly instead of writing a file with a setting silently left
    on its placeholder.
    """
    seen: set[str] = set()
    lines = []
    for line in example.splitlines():
        name = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and name in values:
            lines.append(f"{name}={values[name]}")
            seen.add(name)
        else:
            lines.append(line)
    missing = sorted(set(values) - seen)
    if missing:
        raise SystemExit(f"backend/.env.example has no line for: {', '.join(missing)}")
    return "\n".join(lines) + "\n"


def read_value(path: Path, name: str) -> str | None:
    """The value of `name` in an env file, or None."""
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    return None


def main() -> int:
    backend_env = ROOT / "backend" / ".env"
    compose_env = ROOT / ".env"
    frontend_env = ROOT / "frontend" / ".env"

    if backend_env.exists():
        print(f"kept    {backend_env.relative_to(ROOT)} (already exists, not changed)")
    else:
        # Reuse a database password compose already has, so a re-run after
        # deleting backend/.env does not lock itself out of the database.
        postgres_password = read_value(compose_env, "POSTGRES_PASSWORD") or secrets.token_hex(16)
        admin_password = secrets.token_urlsafe(18)
        example = (ROOT / "backend" / ".env.example").read_text(encoding="utf-8")
        backend_env.write_text(
            render_backend_env(
                example,
                {
                    "SECRET_KEY": secrets.token_hex(32),
                    "CREDENTIAL_ENCRYPTION_KEYS": new_credential_key_ring(),
                    "ADMIN_PASSWORD": admin_password,
                    "DATABASE_URL": DATABASE_URL_TEMPLATE.format(password=postgres_password),
                    "PUBLIC_URL": "http://localhost:8000",
                    # A placeholder here would make the app report the key as configured.
                    "ANTHROPIC_API_KEY": "",
                },
            ),
            encoding="utf-8",
            newline="\n",
        )
        print(f"created {backend_env.relative_to(ROOT)}")
        print("        first sign-in: admin@example.com")
        print(f"        password:      {admin_password}")
        print("        (also in backend/.env as ADMIN_PASSWORD; used once, on an empty database)")

        if read_value(compose_env, "POSTGRES_PASSWORD") is None:
            with compose_env.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    "# Written by scripts/dev_env.py for docker-compose.yml (local development).\n"
                    f"POSTGRES_PASSWORD={postgres_password}\n"
                )
            print(f"wrote   POSTGRES_PASSWORD to {compose_env.relative_to(ROOT)}")

    if frontend_env.exists():
        print(f"kept    {frontend_env.relative_to(ROOT)} (already exists, not changed)")
    else:
        example = (ROOT / "frontend" / ".env.example").read_text(encoding="utf-8")
        frontend_env.write_text(example, encoding="utf-8", newline="\n")
        print(f"created {frontend_env.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
