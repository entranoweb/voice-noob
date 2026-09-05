#!/usr/bin/env python
"""Post a Telnyx-signed TeXML webhook at a running instance.

The inbound path cannot be exercised with curl: `/webhooks/telnyx/voice`
verifies an ed25519 signature over `{timestamp}|{body}` before it does anything,
so an unsigned request is a 403 and tells you nothing about the code you
changed. This signs one the way Telnyx does.

Generate a keypair, give the instance the public half as TELNYX_PUBLIC_KEY, and
post with the private half:

    python signed_webhook.py --keygen            # writes ./telnyx_rehearsal_key
    export TELNYX_PUBLIC_KEY=$(cat telnyx_rehearsal_key.pub)
    # ... start the app with that key ...
    python signed_webhook.py --url http://127.0.0.1:8000 --to +15551234567

What to look for in the answer document:
  - <Connect><Stream url="wss://<your PUBLIC_URL host>/..."> — the host, not the
    scheme; the scheme is always wss and proves nothing
  - bidirectionalMode="rtp" — on the mp3 default the caller hears silence
  - a <Say>no agent is configured</Say> means the number resolved to no agent

The --host and --x-forwarded-proto defaults imitate a TLS-terminating proxy, so
a deployment that depends on request.base_url fails here the way it would fail
in production.
"""

from __future__ import annotations

import argparse
import base64
import sys
import time
from pathlib import Path
from urllib.parse import quote

try:
    import httpx
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:  # pragma: no cover - the backend venv has both
    sys.exit("run this inside the backend environment: uv run python signed_webhook.py")

KEY = Path("telnyx_rehearsal_key")


def keygen() -> None:
    key = Ed25519PrivateKey.generate()
    raw = serialization.Encoding.Raw
    KEY.write_text(
        base64.b64encode(
            key.private_bytes(raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        ).decode()
    )
    KEY.with_suffix(".pub").write_text(
        base64.b64encode(
            key.public_key().public_bytes(raw, serialization.PublicFormat.Raw)
        ).decode()
    )
    print(f"wrote {KEY} and {KEY.with_suffix('.pub')}")
    print("this stands in for Telnyx; it is not a Telnyx key and never leaves your machine")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keygen", action="store_true", help="write a rehearsal keypair and exit")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--to", default="+15551234567", help="the number dialled, in E.164")
    parser.add_argument("--from", dest="caller", default="+15559876543")
    parser.add_argument("--call-sid", default=f"v3:rehearsal-{int(time.time())}")
    parser.add_argument("--host", default="api.synthiqvoice.com", help="imitates the proxy's Host")
    parser.add_argument("--key", default=str(KEY))
    args = parser.parse_args()

    if args.keygen:
        keygen()
        return 0

    key_path = Path(args.key)
    if not key_path.exists():
        return int(bool(sys.stderr.write(f"no key at {key_path}; run with --keygen first\n"))) or 1

    private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(key_path.read_text()))
    body = (
        f"CallSid={quote(args.call_sid, safe='')}&AccountSid=acct"
        f"&From={quote(args.caller, safe='')}&To={quote(args.to, safe='')}"
        f"&CallStatus=ringing&Direction=inbound"
    ).encode()
    timestamp = str(int(time.time()))
    signature = base64.b64encode(private.sign(f"{timestamp}|".encode() + body)).decode()

    try:
        response = httpx.post(
            f"{args.url.rstrip('/')}/webhooks/telnyx/voice",
            content=body,
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "telnyx-signature-ed25519": signature,
                "telnyx-timestamp": timestamp,
                "host": args.host,
                "x-forwarded-proto": "https",
            },
            timeout=20,
        )
    except httpx.HTTPError as exc:
        # A bundled tool that answers an unreachable host with a stack trace
        # sends the reader debugging the tool instead of the deployment.
        print(f"could not reach {args.url}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"HTTP {response.status_code}\n{response.text.strip()}")
    return 0 if response.status_code == 200 else 1  # noqa: PLR2004


if __name__ == "__main__":
    sys.exit(main())
