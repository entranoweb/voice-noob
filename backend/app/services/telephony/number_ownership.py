"""Proof that a phone number is on the registering workspace's own carrier account.

Inbound routing resolves a dialled number to whichever account registered it, so
registering is the moment a number is claimed. Without a proof, anyone who knew a
public number and registered it first could take that line's calls. The proof is
a lookup through the workspace's own carrier credentials: a number that is not
on that account is refused.

Every carrier call goes through a `CarrierNumberLookup`, which a route gets from
the `get_carrier_lookup_factory` dependency. Tests and the report script pass
their own, so nothing here needs a network to be exercised.

The check fails closed: a carrier error, a timeout or an unreadable answer
refuses the registration, with its own message so the user knows to retry.
"""

from __future__ import annotations

import re
import uuid  # noqa: TC003 - dataclass field annotation
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import httpx
import structlog
from fastapi import HTTPException
from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.user_settings import UserSettings

logger = structlog.get_logger()

CARRIER_TIMEOUT_SECONDS = 10.0

NOT_ON_ACCOUNT = "That phone number was not found on this workspace's carrier account."
NO_CREDENTIALS = (
    "Add this workspace's {provider} credentials in Settings before registering a phone number."
)
CREDENTIALS_REJECTED = "The carrier rejected the {provider} credentials saved for this workspace."
UNVERIFIABLE = "Could not verify the phone number with the carrier, try again."
NO_WORKSPACE = "Choose a workspace for this phone number."
BAD_NUMBER = "Enter the phone number with its country code, for example +15550100000."
BAD_PROVIDER = "Invalid provider. Use 'twilio' or 'telnyx'."

_E164 = re.compile(r"^\+?\d{7,15}$")


class CarrierLookupError(Exception):
    """The carrier could not answer: network error, timeout, 5xx or unreadable body."""


class CarrierCredentialsRejectedError(CarrierLookupError):
    """The carrier answered 401 or 403: the saved credentials do not work."""


class CarrierNumberLookup(Protocol):
    """Whether a number is on the carrier account these credentials belong to."""

    async def holds_number(self, number: str) -> bool:
        """True if the account holds `number` (E.164 with plus). Raises CarrierLookupError."""
        ...


CarrierLookupFactory = Callable[[str, "UserSettings | None"], CarrierNumberLookup | None]


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _check_status(response: httpx.Response) -> None:
    if response.status_code in (401, 403):
        raise CarrierCredentialsRejectedError(f"carrier answered {response.status_code}")
    if response.status_code != httpx.codes.OK:
        raise CarrierLookupError(f"carrier answered {response.status_code}")


class TelnyxNumberLookup:
    """`POST /v2/phone_numbers/actions/verify_ownership`.

    The endpoint takes E.164 numbers and returns `found` (numbers the key's
    account owns and can edit) and `not_found`. Chosen over listing with
    `filter[phone_number]` because the list filter is documented only as "at
    least three digits", which reads as a partial match; this one is exact.
    """

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self._api_key = api_key
        self._client = client

    async def holds_number(self, number: str) -> bool:
        try:
            if self._client is not None:
                response = await self._post(self._client, number)
            else:
                async with httpx.AsyncClient(timeout=CARRIER_TIMEOUT_SECONDS) as client:
                    response = await self._post(client, number)
            _check_status(response)
            found = response.json()["data"]["found"]
            return any(_digits(str(item["number_val_e164"])) == _digits(number) for item in found)
        except CarrierLookupError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise CarrierLookupError(type(exc).__name__) from exc

    async def _post(self, client: httpx.AsyncClient, number: str) -> httpx.Response:
        return await client.post(
            "https://api.telnyx.com/v2/phone_numbers/actions/verify_ownership",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={"phone_numbers": [number]},
        )


class TwilioNumberLookup:
    """`GET /2010-04-01/Accounts/{sid}/IncomingPhoneNumbers.json?PhoneNumber=...`.

    Twilio treats `PhoneNumber` as a partial match (digits and `*` wildcards),
    so the answer is compared number by number rather than trusted to be exact.
    """

    def __init__(
        self, account_sid: str, auth_token: str, client: httpx.AsyncClient | None = None
    ) -> None:
        self._sid = account_sid
        self._token = auth_token
        self._client = client

    async def holds_number(self, number: str) -> bool:
        try:
            if self._client is not None:
                response = await self._get(self._client, number)
            else:
                async with httpx.AsyncClient(timeout=CARRIER_TIMEOUT_SECONDS) as client:
                    response = await self._get(client, number)
            _check_status(response)
            items = response.json()["incoming_phone_numbers"]
            return any(_digits(str(item["phone_number"])) == _digits(number) for item in items)
        except CarrierLookupError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise CarrierLookupError(type(exc).__name__) from exc

    async def _get(self, client: httpx.AsyncClient, number: str) -> httpx.Response:
        return await client.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{self._sid}/IncomingPhoneNumbers.json",
            params={"PhoneNumber": number, "PageSize": "1000"},
            auth=(self._sid, self._token),
        )


def default_lookup_factory(
    provider: str, settings: UserSettings | None
) -> CarrierNumberLookup | None:
    """The real lookup for `provider`, or None when the workspace has no credentials."""
    if settings is None:
        return None
    if provider == "telnyx" and settings.telnyx_api_key:
        return TelnyxNumberLookup(settings.telnyx_api_key)
    if provider == "twilio" and settings.twilio_account_sid and settings.twilio_auth_token:
        return TwilioNumberLookup(settings.twilio_account_sid, settings.twilio_auth_token)
    return None


def get_carrier_lookup_factory() -> CarrierLookupFactory:
    """FastAPI dependency; tests override it with a fake carrier."""
    return default_lookup_factory


def normalise_number(number: str) -> str:
    """E.164 with the plus, or a 422 when this cannot be a dialled number."""
    candidate = number.strip()
    if not _E164.match(candidate):
        raise HTTPException(status_code=422, detail=BAD_NUMBER)
    return "+" + candidate.lstrip("+")


async def verify_number_on_workspace_account(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    provider: str,
    number: str,
    factory: CarrierLookupFactory,
) -> None:
    """Return normally only when the carrier confirms the number is on the account.

    Credentials are those of `workspace_id`, read the way every other carrier
    call reads them. There is no user-level fallback: a number with no
    workspace has no carrier account to be proven on, so it is refused.
    422 when the number is not on the account, there is no workspace or nothing
    to check it with; 503 when the carrier could not be asked. Either way
    nothing is registered.
    """
    from app.api.settings import get_user_api_keys

    if provider not in ("telnyx", "twilio"):
        raise HTTPException(status_code=422, detail=BAD_PROVIDER)
    if workspace_id is None:
        raise HTTPException(status_code=422, detail=NO_WORKSPACE)
    e164 = normalise_number(number)

    settings = await get_user_api_keys(user_id, db, workspace_id=workspace_id)
    lookup = factory(provider, settings)
    if lookup is None:
        raise HTTPException(status_code=422, detail=NO_CREDENTIALS.format(provider=provider))

    try:
        held = await lookup.holds_number(e164)
    except CarrierCredentialsRejectedError as exc:
        raise HTTPException(
            status_code=422, detail=CREDENTIALS_REJECTED.format(provider=provider)
        ) from exc
    except Exception as exc:
        # Fail closed. Any error at all is "could not verify", never "allowed".
        logger.warning("number_verification_failed", provider=provider, error=type(exc).__name__)
        raise HTTPException(
            status_code=503, detail=UNVERIFIABLE, headers={"Retry-After": "30"}
        ) from exc

    if not held:
        raise HTTPException(status_code=422, detail=NOT_ON_ACCOUNT)


@dataclass(frozen=True)
class UnverifiedNumber:
    """A registered number that registration would refuse today, and why."""

    phone_number_id: uuid.UUID
    workspace_id: uuid.UUID | None
    provider: str
    phone_number: str
    reason: str


async def find_unverified_numbers(
    db: AsyncSession, *, factory: CarrierLookupFactory
) -> list[UnverifiedNumber]:
    """Live registrations that do not pass the check a new registration must pass.

    Read-only: nothing is written or changed. It asks the same question, with the
    same code, as `verify_number_on_workspace_account`, so the list is exactly
    what registration would refuse. Released numbers are skipped, as routing
    skips them. A number whose carrier could not be asked is listed too, with
    that reason, because "unchecked" is not "fine".
    """
    from app.models.phone_number import PhoneNumber, PhoneNumberStatus

    records = (
        (
            await db.execute(
                select(PhoneNumber)
                .where(PhoneNumber.status != PhoneNumberStatus.RELEASED.value)
                .order_by(PhoneNumber.workspace_id, PhoneNumber.created_at, PhoneNumber.id)
            )
        )
        .scalars()
        .all()
    )
    failures: list[UnverifiedNumber] = []
    for record in records:
        try:
            await verify_number_on_workspace_account(
                db,
                user_id=record.user_id,
                workspace_id=record.workspace_id,
                provider=record.provider,
                number=record.phone_number,
                factory=factory,
            )
        except HTTPException as exc:
            failures.append(
                UnverifiedNumber(
                    phone_number_id=record.id,
                    workspace_id=record.workspace_id,
                    provider=record.provider,
                    phone_number=record.phone_number,
                    reason=str(exc.detail),
                )
            )
    return failures


@dataclass(frozen=True)
class UnregisteredAgentNumber:
    """An agent whose `phone_number_id` is not a live registered number."""

    agent_id: uuid.UUID
    user_id: int
    reference: str


async def find_unregistered_agent_numbers(db: AsyncSession) -> list[UnregisteredAgentNumber]:
    """Agents whose `phone_number_id` has no live registered record.

    Read-only, and no carrier is asked. Routing still honours the field for
    agents that predate registration, so this is the list of agents whose number
    nobody ever proved. A diallable value is matched on the number itself; any
    other value on a record's carrier id or id, as `ensure_phone_reference_is_owned`
    does.
    """
    from app.core.ownership import reference_condition
    from app.models.agent import Agent
    from app.models.phone_number import PhoneNumber, PhoneNumberStatus

    agents = (
        (
            await db.execute(
                select(Agent)
                .where(Agent.phone_number_id.is_not(None), Agent.phone_number_id != "")
                .order_by(Agent.created_at, Agent.id)
            )
        )
        .scalars()
        .all()
    )
    live = PhoneNumber.status != PhoneNumberStatus.RELEASED.value
    listed: list[UnregisteredAgentNumber] = []
    for agent in agents:
        reference = (agent.phone_number_id or "").strip()
        if not reference:
            continue
        condition = reference_condition(reference)
        found = (await db.execute(select(PhoneNumber.id).where(condition, live).limit(1))).first()
        if found is None:
            listed.append(
                UnregisteredAgentNumber(
                    agent_id=agent.id, user_id=agent.user_id, reference=reference
                )
            )
    return listed
