# Rehearsing a call without a carrier

Reproduces the inbound path on one machine, far enough to see the real answer
document and the real database row. Everything up to OpenAI is covered; the
Realtime leg needs a genuine key and the audio needs a genuine number.

Read this when you have changed the call path and want evidence rather than an
argument. It takes about five minutes.

## 1. Postgres and Redis

Docker is the normal route (`docker compose up postgres redis`). Where there is
no daemon, native binaries work — Postgres refuses to run as root, so it needs
an unprivileged owner and a socket directory it can write:

```bash
useradd -m pgrunner 2>/dev/null
mkdir -p /home/pgrunner/pgdata /var/run/postgresql
chown -R pgrunner /home/pgrunner /var/run/postgresql
su pgrunner -c "/usr/lib/postgresql/*/bin/initdb -D /home/pgrunner/pgdata -U postgres --auth=trust"
su pgrunner -c "/usr/lib/postgresql/*/bin/pg_ctl -D /home/pgrunner/pgdata -l /home/pgrunner/pg.log -o '-p 5432 -h 127.0.0.1' start"
psql -h 127.0.0.1 -U postgres -c "CREATE DATABASE voicenoob;"
redis-server --daemonize yes --port 6379
```

The missing socket directory is the failure that looks like Postgres is broken:
`could not create lock file "/var/run/postgresql/.s.PGSQL.5432.lock"`.

## 2. Environment

Export these rather than writing a `.env` — sourcing a file strips the quotes
from `CORS_ORIGINS` and pydantic then rejects it, which reads like a config bug
in the app and is not one.

```bash
export DATABASE_URL='postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/voicenoob'
export REDIS_URL='redis://127.0.0.1:6379/0'
export SECRET_KEY='0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
export ADMIN_EMAIL='admin@synthiqvoice.com' ADMIN_PASSWORD='RehearsalPassw0rd!' ADMIN_NAME='Admin'
export CORS_ORIGINS='["https://app.synthiqvoice.com"]'
export DEBUG=false
# The value under test: the answer document is built from this, and setting it
# to something the request could not have produced is what proves it is used.
export PUBLIC_URL='https://api.synthiqvoice.com'
```

Then the signing key the instance will verify against:

```bash
cd backend
uv run python ../.claude/skills/synthiq-voice-calls/scripts/signed_webhook.py --keygen
export TELNYX_PUBLIC_KEY=$(cat telnyx_rehearsal_key.pub)
```

## 3. Migrate and run

```bash
uv run alembic upgrade head      # ends at the single head, currently b83d1c4f7a90
uv run gunicorn app.main:app -c gunicorn.conf.py --bind 127.0.0.1:8000
```

Use gunicorn, not `uvicorn --reload`: it is what the container runs, and
`gunicorn.conf.py` is part of what you are testing.

Do not stop it with `pkill -f "gunicorn app.main:app"` — the pattern matches
your own shell's command line and kills the terminal running it.

## 4. An agent on a number, through the API

Do this through the API rather than SQL. Writing `agents.phone_number_id`
directly exercises a route the product cannot produce and hides the routing
bug it was written to catch.

```bash
TOK=$(curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'content-type: application/x-www-form-urlencoded' \
  --data-urlencode 'username=admin@synthiqvoice.com' \
  --data-urlencode 'password=RehearsalPassw0rd!' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

AG=$(curl -s -X POST http://127.0.0.1:8000/api/v1/agents -H "authorization: Bearer $TOK" \
  -H 'content-type: application/json' \
  -d '{"name":"Rehearsal","system_prompt":"You book appointments.","voice":"marin","language":"en-US","pricing_tier":"premium"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

PN=$(curl -s -X POST http://127.0.0.1:8000/api/v1/phone-numbers -H "authorization: Bearer $TOK" \
  -H 'content-type: application/json' \
  -d '{"phone_number":"+15551234567","provider":"telnyx","provider_id":"rehearsal-1"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

curl -s -X PUT "http://127.0.0.1:8000/api/v1/phone-numbers/$PN" -H "authorization: Bearer $TOK" \
  -H 'content-type: application/json' -d "{\"assigned_agent_id\":\"$AG\"}" > /dev/null
```

The admin user is created on first boot from `ADMIN_*`. Login is form-encoded
with `username`, not JSON with `email`.

## 5. Place the call

```bash
uv run python ../.claude/skills/synthiq-voice-calls/scripts/signed_webhook.py \
  --url http://127.0.0.1:8000 --to +15551234567
```

A working path returns `<Connect><Stream url="wss://api.synthiqvoice.com/...">`
with `bidirectionalMode="rtp"`. Read the **host**: it came from `PUBLIC_URL`,
and no request to `127.0.0.1` could have produced it, which is the proof.

Then the row:

```bash
psql -h 127.0.0.1 -U postgres -d voicenoob \
  -c "select provider_call_id, direction, status, from_number, to_number from call_records;"
```

## 6. The media stream

```bash
uv run python - <<'EOF'
import asyncio, websockets
from websockets.exceptions import ConnectionClosed
async def probe(path):
    try:
        async with websockets.connect(f"ws://127.0.0.1:8000/ws/telephony/telnyx/{path}") as ws:
            print(await asyncio.wait_for(ws.recv(), timeout=8))
    except ConnectionClosed as c:
        print("closed", c.rcvd.code if c.rcvd else "?", c.rcvd.reason if c.rcvd else "")
asyncio.run(probe("00000000-0000-0000-0000-000000000000?direction=inbound"))
EOF
```

`4004 Agent not found` is success: the bridge resolved the route, accepted the
upgrade and queried the database. That is the same signal the preflight uses.

A real agent id closes without a code and logs `workspace_missing_settings` —
correct, and the boundary of this rehearsal. Past that point you need a real
OpenAI key in the workspace, and then a real number to hear anything.

## 7. Run the suite against Postgres while you are here

```bash
createdb -h 127.0.0.1 -U postgres voicenoob_test
TEST_DATABASE_URL='postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/voicenoob_test' uv run pytest -q --no-cov
```

Green. The 16 failures the sqlite fallback produces are artifacts of sqlite, and
knowing that is worth the two minutes.
