"""Destination checks for addresses a customer supplies and this server calls.

Three settings let an account point the backend at a URL of its choosing: the
QA alert webhooks, the Azure OpenAI endpoint and the Shopify shop domain. A
server that fetches whatever it is given can be aimed at its own network: the
database, Redis, another container, or a cloud metadata service that hands out
credentials.

`assert_public_url` is the check: HTTPS only, no credentials in the URL, and
every address the host resolves to must be a public one. It is called when the
value is saved (so a mistake is reported to the person making it) and again
immediately before each request (so a name that has since been re-pointed at a
private address is refused). Requests made after the check do not follow
redirects, since a redirect is a second destination nobody checked.

What this does not close: the name is resolved once here and again by the HTTP
client, so a DNS answer that changes between the two (rebinding) gets through.
Closing that means pinning the connection to the checked address.

An operator who genuinely needs a private destination (an Azure private
endpoint, say) lists its exact host name in OUTBOUND_ALLOW_PRIVATE_HOSTS.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from app.core.config import settings


class UnsafeDestinationError(ValueError):
    """The address is not one this server will call."""


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    # ::ffff:10.0.0.1 is 10.0.0.1 for every purpose that matters here.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # is_global is false for private, loopback, link-local (which covers the
    # 169.254.169.254 metadata address), shared, reserved, multicast and
    # unspecified ranges.
    return ip.is_global and not ip.is_multicast


def parse_https_host(url: str) -> str:
    """The host of an HTTPS URL that carries no credentials; refuse anything else."""
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise UnsafeDestinationError("That is not a valid URL") from exc
    if parts.scheme != "https":
        raise UnsafeDestinationError("The address must start with https://")
    if parts.username or parts.password:
        raise UnsafeDestinationError("The address must not contain a user name or password")
    if not parts.hostname:
        raise UnsafeDestinationError("The address has no host name")
    return parts.hostname.lower()


async def _resolve(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeDestinationError("The host name does not resolve") from exc
    return [str(info[4][0]) for info in infos]


async def assert_public_host(host: str) -> None:
    """Refuse a host that is, or resolves to, anything but public addresses."""
    host = host.strip().lower().rstrip(".")
    if host in {h.lower() for h in settings.OUTBOUND_ALLOW_PRIVATE_HOSTS}:
        return
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        addresses = await _resolve(host)
    if not addresses:
        raise UnsafeDestinationError("The host name does not resolve")
    for address in addresses:
        if not _is_public(address):
            raise UnsafeDestinationError("The address points inside a private network")


async def assert_public_url(url: str) -> str:
    """Check an HTTPS URL's destination and return the URL, stripped."""
    host = parse_https_host(url)
    await assert_public_host(host)
    return url.strip()
