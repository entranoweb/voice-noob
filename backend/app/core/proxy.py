"""Which peers may tell the server who the real client is.

Behind a reverse proxy the socket peer is the proxy, so the rate limiter and the
audit trail would see one address for every client. The proxy says who the
client was in ``X-Forwarded-For``; that header is only believable when the peer
that sent it is the proxy and not the client itself.

How it is wired, so nobody has to rediscover it. ``gunicorn.conf.py`` selects
``uvicorn.workers.UvicornWorker`` and gives it ``forwarded_allow_ips``. uvicorn
wraps the app in ``ProxyHeadersMiddleware`` with that list: when the connecting
peer is in it, ``scope["client"]`` becomes the right-most address in
``X-Forwarded-For`` that is not itself trusted. Everything downstream
(``request.client``, slowapi's ``get_remote_address``, ``audit.client_ip``) reads
that one rewritten value, so they always agree. A peer not in the list is
believed about nothing.

``TRUSTED_PROXY_IPS`` is the one setting. It is a comma-separated list of
addresses and CIDR networks. Unset, only loopback is trusted. ``*`` trusts every
peer, which lets any client choose its own address (and so its own rate-limit
bucket); it is accepted only as the whole value and never by default.

Under ``*`` the first ``X-Forwarded-For`` entry is taken, and the client wrote
it. ``stored_client_address`` therefore treats ``request.client.host`` as
untrusted text: it keeps a valid IP address and replaces anything else with a
marker, so a hostile header can neither overflow a column nor be stored as a
string.
"""

from __future__ import annotations

import ipaddress

DEFAULT_TRUSTED_PROXY_IPS = "127.0.0.1,::1"
SETTING_NAME = "TRUSTED_PROXY_IPS"

# Narrowest address column: ConsentRecord.ip_address is String(45), the length
# of the longest IPv6 text form (an IPv4-mapped address with a dotted tail).
ADDRESS_COLUMN_WIDTH = 45
INVALID_ADDRESS = "invalid"


def stored_client_address(host: str) -> str:
    """The address to persist for ``host``: a valid IP that fits, else a marker.

    Never raises.
    """
    if len(host) <= ADDRESS_COLUMN_WIDTH:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return host
    return INVALID_ADDRESS


def trusted_proxy_ips(raw: str | None) -> str:
    """Validate the setting and return it in the form gunicorn takes.

    Raises ``ValueError`` naming the setting on a malformed entry: uvicorn
    would otherwise treat it as a literal that matches no peer, and the proxy
    would silently stop being trusted.
    """
    if raw is None or not raw.strip():
        return DEFAULT_TRUSTED_PROXY_IPS
    entries = [part.strip() for part in raw.split(",")]
    if entries == ["*"]:
        return "*"
    for entry in entries:
        try:
            ipaddress.ip_network(entry, strict=True)
        except ValueError as exc:
            raise ValueError(
                f"{SETTING_NAME} entry {entry!r} is not an IP address or network "
                f"(got {raw!r}); use e.g. 172.18.0.0/16, 10.0.0.5 or '*'"
            ) from exc
    return ",".join(entries)
