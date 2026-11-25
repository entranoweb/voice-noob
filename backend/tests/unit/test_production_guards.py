"""What a production instance refuses: shipped credentials, private destinations,
stale webhooks, tools nobody enabled, and operational pages served to strangers.
"""

from __future__ import annotations

import base64
import importlib.util
import socket
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import pytest
import sqlalchemy as sa
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.auth import get_password_hash, verify_password
from app.core import safe_http
from app.core.config import (
    InsecureDefaultCredentialsError,
    Settings,
    is_shipped_admin_password,
    refuse_default_credentials,
    settings,
)
from app.core.first_admin import ensure_first_admin
from app.core.safe_http import UnsafeDestinationError, assert_public_host, assert_public_url
from app.core.webhook_security import telnyx_timestamp_is_fresh, validate_telnyx_signature
from app.main import app
from app.models.user import User
from app.monitoring.metrics import metrics_request_allowed
from app.services.gpt_realtime import GPTRealtimeSession
from app.services.tools.registry import ToolRegistry
from app.services.tools.shopify_tools import normalise_shop_domain

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

REPO = Path(__file__).resolve().parents[3]
KEY = "a-real-signing-key-for-this-test-only"


class TestShippedServicePasswords:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"DATABASE_URL": "postgresql+asyncpg://postgres:changeme_in_production@db:5432/app"},
            {"REDIS_URL": "redis://:changeme_in_production@redis:6379/0"},
            {"REDIS_PASSWORD": "your_secure_redis_password_here"},
        ],
    )
    def test_production_refuses_to_start_on_one(self, overrides: dict[str, str]) -> None:
        candidate = Settings(_env_file=None, SECRET_KEY=KEY, DEBUG=False, **overrides)  # type: ignore[call-arg]

        with pytest.raises(InsecureDefaultCredentialsError) as refused:
            refuse_default_credentials(candidate)

        assert "changeme" not in str(refused.value)

    def test_debug_and_real_passwords_start(self) -> None:
        shipped = "postgresql+asyncpg://postgres:changeme_in_production@db:5432/app"
        refuse_default_credentials(
            Settings(_env_file=None, SECRET_KEY=KEY, DEBUG=True, DATABASE_URL=shipped)  # type: ignore[call-arg]
        )
        refuse_default_credentials(
            Settings(  # type: ignore[call-arg]
                _env_file=None,
                SECRET_KEY=KEY,
                DEBUG=False,
                DATABASE_URL="postgresql+asyncpg://app:Vq7-long-random-value@db:5432/app",
                REDIS_URL="redis://:another-long-random-value@redis:6379/0",
            )
        )


class TestFirstAdministrator:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("shipped", ["changeme", "CHANGE_ME_IN_PRODUCTION"])
    async def test_it_is_not_created_from_a_shipped_password(
        self, test_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, shipped: str
    ) -> None:
        monkeypatch.setattr(settings, "ADMIN_PASSWORD", shipped)

        with pytest.raises(InsecureDefaultCredentialsError):
            await ensure_first_admin(test_session)

        users = (await test_session.execute(select(User))).scalars().all()
        assert users == []

    @pytest.mark.asyncio
    async def test_it_is_created_from_a_real_password(
        self, test_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "ADMIN_EMAIL", "owner@example.com")
        monkeypatch.setattr(settings, "ADMIN_PASSWORD", "a-long-password-of-our-own")

        await ensure_first_admin(test_session)

        admin = (await test_session.execute(select(User))).scalar_one()
        assert admin.is_superuser is True
        assert verify_password("a-long-password-of-our-own", admin.hashed_password)

    @pytest.mark.asyncio
    async def test_an_existing_administrator_on_a_shipped_password_stops_the_start(
        self, test_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        test_session.add(
            User(
                email="old-admin@example.com",
                hashed_password=get_password_hash("changeme"),
                is_active=True,
                is_superuser=True,
            )
        )
        await test_session.commit()
        monkeypatch.setattr(settings, "ADMIN_EMAIL", "someone-else@example.com")
        monkeypatch.setattr(settings, "ADMIN_PASSWORD", "a-long-password-of-our-own")

        with pytest.raises(InsecureDefaultCredentialsError):
            await ensure_first_admin(test_session)

    @pytest.mark.asyncio
    async def test_setting_a_new_password_for_that_account_replaces_it(
        self, test_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        test_session.add(
            User(
                email="old-admin@example.com",
                hashed_password=get_password_hash("changeme"),
                is_active=True,
                is_superuser=True,
            )
        )
        await test_session.commit()
        monkeypatch.setattr(settings, "ADMIN_EMAIL", "old-admin@example.com")
        monkeypatch.setattr(settings, "ADMIN_PASSWORD", "a-long-password-of-our-own")

        await ensure_first_admin(test_session)

        admin = (await test_session.execute(select(User))).scalar_one()
        assert not verify_password("changeme", admin.hashed_password)
        assert verify_password("a-long-password-of-our-own", admin.hashed_password)


class TestProductionComposeFile:
    """The file itself: nothing in it may work as a credential, or bypass the proxy."""

    @pytest.fixture
    def compose(self) -> dict[str, Any]:
        return yaml.safe_load((REPO / "docker-compose.prod.yml").read_text(encoding="utf-8"))

    def test_no_credential_has_a_default(self, compose: dict[str, Any]) -> None:
        text = (REPO / "docker-compose.prod.yml").read_text(encoding="utf-8")
        backend = compose["services"]["backend"]["environment"]

        assert "changeme" not in text.lower()
        for name in ("ADMIN_EMAIL", "ADMIN_PASSWORD", "REDIS_PASSWORD", "SECRET_KEY"):
            assert ":?" in backend[name], name
        assert ":?" in compose["services"]["postgres"]["environment"]["POSTGRES_PASSWORD"]
        assert "POSTGRES_PASSWORD:?" in backend["DATABASE_URL"]

    def test_the_app_ports_are_not_published_on_the_host(self, compose: dict[str, Any]) -> None:
        for service in ("backend", "frontend", "postgres", "redis"):
            assert "ports" not in compose["services"][service], service

    def test_sign_up_and_docs_are_closed_unless_asked_for(self, compose: dict[str, Any]) -> None:
        backend = compose["services"]["backend"]["environment"]

        assert backend["ALLOW_PUBLIC_SIGNUP"] == "${ALLOW_PUBLIC_SIGNUP:-false}"
        assert backend["ENABLE_API_DOCS"] == "${ENABLE_API_DOCS:-false}"

    def test_the_backend_image_does_not_run_as_root(self) -> None:
        lines = (REPO / "backend" / "Dockerfile").read_text(encoding="utf-8").splitlines()
        users = [line.split()[1] for line in lines if line.startswith("USER ")]

        assert users, "no USER instruction"
        assert users[-1] not in {"root", "0"}


class TestDestinationChecks:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "10.1.2.3",
            "172.16.0.9",
            "192.168.1.1",
            "169.254.169.254",
            "100.64.0.1",
            "0.0.0.0",
            "::1",
            "fe80::1",
            "fd00::1",
            "::ffff:10.0.0.1",
        ],
    )
    async def test_private_loopback_link_local_and_metadata_addresses_are_refused(
        self, host: str
    ) -> None:
        with pytest.raises(UnsafeDestinationError):
            await assert_public_host(host)

    @pytest.mark.asyncio
    async def test_a_name_that_resolves_inside_the_network_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def resolve(_host: str) -> list[str]:
            return ["93.184.216.34", "10.0.0.7"]

        monkeypatch.setattr(safe_http, "_resolve", resolve)

        with pytest.raises(UnsafeDestinationError):
            await assert_public_url("https://looks-harmless.example.com/hook")

    @pytest.mark.asyncio
    async def test_a_public_destination_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def resolve(_host: str) -> list[str]:
            return ["93.184.216.34"]

        monkeypatch.setattr(safe_http, "_resolve", resolve)

        assert await assert_public_url(" https://hooks.example.com/x ") == (
            "https://hooks.example.com/x"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "http://hooks.example.com/x",
            "https://user:pw@hooks.example.com/x",
            "ftp://x",
            "nonsense",
        ],
    )
    async def test_only_plain_https_urls_are_accepted(self, url: str) -> None:
        with pytest.raises(UnsafeDestinationError):
            await assert_public_url(url)

    @pytest.mark.asyncio
    async def test_a_name_that_does_not_resolve_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fail(*_: Any, **__: Any) -> Any:
            raise socket.gaierror

        loop = __import__("asyncio").get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", fail)

        with pytest.raises(UnsafeDestinationError):
            await assert_public_host("no-such-host.invalid")

    @pytest.mark.asyncio
    async def test_an_operator_can_allow_a_named_private_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "OUTBOUND_ALLOW_PRIVATE_HOSTS", ["azure.internal.example"])

        await assert_public_host("azure.internal.example")

    @pytest.mark.asyncio
    async def test_the_alert_webhook_is_not_called_when_it_points_inside(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services.qa import alerts

        called: list[str] = []

        class _Client:
            def __init__(self, *_: Any, **__: Any) -> None:
                pass

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            async def post(self, url: str, **_: Any) -> Any:
                called.append(url)
                raise AssertionError("the request was sent")

        monkeypatch.setattr(alerts.httpx, "AsyncClient", _Client)

        with pytest.raises(UnsafeDestinationError):
            await alerts._send_webhook_alert("https://169.254.169.254/latest", {"k": "v"})

        assert called == []

    @pytest.mark.parametrize(
        "domain",
        [
            "evil.example.com",
            "127.0.0.1",
            "shop.myshopify.com.evil.example",
            "shop.myshopify.com/redirect?to=http://10.0.0.1",
            "shop.myshopify.com:8443",
            "internal-service",
        ],
    )
    def test_a_shop_domain_must_be_a_shopify_address(self, domain: str) -> None:
        with pytest.raises(ValueError, match="myshopify"):
            normalise_shop_domain(domain)

    def test_a_real_shop_domain_is_kept(self) -> None:
        assert normalise_shop_domain("https://My-Store.myshopify.com/") == "my-store.myshopify.com"


def _telnyx_headers(private: Ed25519PrivateKey, body: bytes, sent_at: int) -> tuple[str, str]:
    signature = private.sign(f"{sent_at}|".encode() + body)
    return base64.b64encode(signature).decode(), str(sent_at)


class TestTelnyxWebhookAge:
    def _keys(self) -> tuple[Ed25519PrivateKey, str]:
        private = Ed25519PrivateKey.generate()
        public = base64.b64encode(
            private.public_key().public_bytes(
                encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
            )
        ).decode()
        return private, public

    def test_a_fresh_signed_webhook_is_accepted(self) -> None:
        private, public = self._keys()
        signature, timestamp = _telnyx_headers(private, b"{}", int(time.time()))

        assert validate_telnyx_signature(signature, timestamp, b"{}", public) is True

    @pytest.mark.parametrize("offset", [-3600, -301, 301, 86400])
    def test_a_genuine_signature_on_an_old_or_future_webhook_is_refused(self, offset: int) -> None:
        """A replay: the signature verifies, the moment has passed."""
        private, public = self._keys()
        signature, timestamp = _telnyx_headers(private, b"{}", int(time.time()) + offset)

        assert validate_telnyx_signature(signature, timestamp, b"{}", public) is False

    def test_a_timestamp_that_is_not_a_number_is_not_fresh(self) -> None:
        assert telnyx_timestamp_is_fresh("yesterday") is False


class TestToolSelectionOnPhoneCalls:
    def test_only_the_tools_the_owner_ticked_are_enabled(self, test_session: AsyncSession) -> None:
        registry = ToolRegistry(test_session, 1)

        names = registry.enabled_tool_names(["crm"], {"crm": ["search_customer"]})

        assert names == {"search_customer"}

    @pytest.mark.asyncio
    async def test_a_tool_the_model_was_not_offered_is_not_executed(
        self, test_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = GPTRealtimeSession(
            test_session,
            1,
            {"enabled_tools": ["crm"], "enabled_tool_ids": {"crm": ["search_customer"]}},
        )
        session.tool_registry = ToolRegistry(test_session, 1)
        executed: list[str] = []

        async def execute(name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
            executed.append(name)
            return {"success": True}

        monkeypatch.setattr(session.tool_registry, "execute_tool", execute)

        refused = await session.handle_tool_call(
            {"name": "cancel_appointment", "arguments": {"appointment_id": 7}}
        )
        allowed = await session.handle_tool_call(
            {"name": "search_customer", "arguments": {"query": "x"}}
        )

        assert refused["success"] is False
        assert allowed["success"] is True
        assert executed == ["search_customer"]


class TestOperationalEndpoints:
    def test_metrics_need_the_token_outside_debug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "METRICS_TOKEN", None)
        assert metrics_request_allowed(None) is False
        assert metrics_request_allowed("Bearer anything") is False

        monkeypatch.setattr(settings, "METRICS_TOKEN", "scrape-secret")
        assert metrics_request_allowed("Bearer wrong") is False
        assert metrics_request_allowed("Bearer scrape-secret") is True

    @pytest.mark.asyncio
    async def test_docs_and_metrics_answer_not_found_to_a_stranger(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert settings.DEBUG is False
        monkeypatch.setattr(settings, "METRICS_TOKEN", "scrape-secret")
        transport = ASGITransport(app=app)  # type: ignore[arg-type]

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            docs = [await client.get(path) for path in ("/docs", "/redoc", "/openapi.json")]
            anonymous = await client.get("/metrics")
            scraper = await client.get(
                "/metrics", headers={"Authorization": "Bearer scrape-secret"}
            )

        assert [r.status_code for r in docs] == [404, 404, 404]
        assert anonymous.status_code == 404
        assert scraper.status_code == 200


class TestOneOwnerMigration:
    """The migration reports duplicates; it does not choose between them."""

    def _migration(self) -> Any:
        path = (
            REPO
            / "backend"
            / "migrations"
            / "versions"
            / "d2f5a8c31b47_one_owner_per_phone_number.py"
        )
        spec = importlib.util.spec_from_file_location("one_owner_migration", path)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_live_duplicates_are_found_across_spellings_and_released_rows_ignored(self) -> None:
        migration = self._migration()
        engine = sa.create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "CREATE TABLE phone_numbers (id TEXT, user_id TEXT, phone_number TEXT, "
                    "status TEXT, created_at TEXT)"
                )
            )
            rows = [
                ("r1", "owner-a", "+442079460001", "active", "1"),
                ("r2", "owner-b", "442079460001", "active", "2"),
                ("r3", "owner-c", "+442079460001", "released", "3"),
                ("r4", "owner-a", "+442079460002", "active", "4"),
            ]
            for row in rows:
                conn.execute(
                    sa.text("INSERT INTO phone_numbers VALUES (:a, :b, :c, :d, :e)"),
                    dict(zip("abcde", row, strict=True)),
                )

            duplicates = migration.find_duplicates(conn)

        assert duplicates == [[("r1", "owner-a"), ("r2", "owner-b")]]


class TestDevelopmentSetupEnvironment:
    """`make setup` must produce an environment the start-up checks accept."""

    def test_the_generated_backend_env_is_not_refused(self) -> None:
        path = REPO / "scripts" / "dev_env.py"
        spec = importlib.util.spec_from_file_location("dev_env", path)
        assert spec is not None
        assert spec.loader is not None
        dev_env = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dev_env)

        example = (REPO / "backend" / ".env.example").read_text(encoding="utf-8")
        # The settings `make setup` generates, as it passes them (scripts/dev_env.py).
        rendered = dev_env.render_backend_env(
            example,
            {
                "SECRET_KEY": "f" * 64,
                "ADMIN_PASSWORD": "a-generated-admin-password",
                "DATABASE_URL": dev_env.DATABASE_URL_TEMPLATE.format(password="e" * 32),
            },
        )
        values = dict(
            line.split("=", 1)
            for line in rendered.splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        )

        candidate = Settings(  # type: ignore[call-arg]
            _env_file=None,
            SECRET_KEY=values["SECRET_KEY"],
            DEBUG=values.get("DEBUG", "false").strip().lower() == "true",
            DATABASE_URL=values["DATABASE_URL"],
            REDIS_URL=values["REDIS_URL"],
        )
        refuse_default_credentials(candidate)
        assert not is_shipped_admin_password(values["ADMIN_PASSWORD"])

    def test_the_example_file_alone_would_be_refused(self) -> None:
        """Which is why setup rewrites it: the example's admin password is public."""
        example = (REPO / "backend" / ".env.example").read_text(encoding="utf-8")
        shipped = next(
            line.split("=", 1)[1]
            for line in example.splitlines()
            if line.startswith("ADMIN_PASSWORD=")
        )

        assert is_shipped_admin_password(shipped)
