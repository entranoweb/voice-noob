# Synthiq Voice Platform - Coolify Deployment Guide

## Overview

- **Stack:** Next.js 15.5 + FastAPI + PostgreSQL 17 + Redis 7
- **Deployment:** Single docker-compose on Coolify (Azure Germany)
- **SSL:** Automatic via Coolify/Traefik

---

## Pre-Deployment Checklist

### 1. Coolify Server Ready
- [ ] Coolify installed on Azure VM (Germany region)
- [ ] `app.synthiqvoice.com` A record pointing to the server (the dashboard)
- [ ] `api.synthiqvoice.com` A record pointing to the server (the backend, and the
      host Telnyx dials — see Step 4)
- [ ] `synthiq.io` is the marketing site and is not involved here

### 2. GitHub Repo
- [ ] Code pushed to GitHub
- [ ] Repo connected to Coolify

### 3. API Keys Ready (add in dashboard after deploy)
- [ ] OpenAI API Key OR Azure OpenAI credentials
- [ ] Telnyx API Key + Phone Number purchased

---

## Step-by-Step Deployment

### Step 1: Push Code to GitHub

```bash
cd voice-noob
git add .
git commit -m "Production Docker setup"
git push origin main
```

### Step 2: Create Coolify Project

1. **Coolify Dashboard** → Projects → **+ New Project**
2. Name: `synthiq-voice`
3. Click **Create**

### Step 3: Add Docker Compose Resource

1. In your project → **+ Add Resource**
2. Select **Docker Compose**
3. Choose **GitHub** as source
4. Select your repository
5. **Docker Compose Location:** `docker-compose.prod.yml`
6. Click **Continue**

### Step 4: Configure Environment Variables

In Coolify's **Environment Variables** section, add:

```env
# Database (REQUIRED - change these!)
POSTGRES_USER=postgres
POSTGRES_PASSWORD=YourSecurePostgresPassword123!
# Whatever you put here wins: compose builds DATABASE_URL from it and passes
# that to the app explicitly, so the app's own default is never consulted. Pick
# a name on a fresh deployment and then leave it alone — pointing an existing
# deployment at a different name gives it an empty database in which every
# table looks missing.
POSTGRES_DB=synthiq_voice

# Redis (REQUIRED - change this!)
REDIS_PASSWORD=YourSecureRedisPassword456!

# Security (REQUIRED - generate a new one!)
# Generate: python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY=your_64_character_hex_string_here

# Encrypts stored customer credentials (REQUIRED - generate one, keep a copy off the server)
# Generate: python -c "import secrets,base64;print('k1:'+base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
CREDENTIAL_ENCRYPTION_KEYS=k1:your_base64_key_here

# Admin User (REQUIRED - change these!)
ADMIN_EMAIL=admin@synthiqvoice.com
ADMIN_PASSWORD=YourSecureAdminPassword789!
ADMIN_NAME=Admin

# The public origin of the BACKEND. Required for a phone call to work.
# The answer document builds the media stream URL from this, and the carrier
# dials it. Unset, it falls back to the request's own host, which behind
# Coolify's proxy is the container address Telnyx cannot reach: the call
# connects, the row lands, and the caller hears silence. See docs/HANDOFF.md
# section 7.2.
PUBLIC_URL=https://api.synthiqvoice.com

# Telnyx webhook signature verification. Optional here.
# A webhook is checked first against the public key of the workspace that owns
# the number or call (entered in the dashboard), then against this one. Set it
# only when every number runs on one platform-wide Telnyx account. With neither
# key the webhook is refused 403 and no call reaches an agent, whatever DEBUG
# says. The only way to accept an unsigned webhook is
# ALLOW_UNSIGNED_WEBHOOKS=true, which must never be set here.
TELNYX_PUBLIC_KEY=

# Only needed for outbound calls and buying numbers. Inbound does not use it.
TELNYX_API_KEY=your_telnyx_api_key

# REQUIRED in production. It skips the startup checks on SECRET_KEY and
# CREDENTIAL_ENCRYPTION_KEYS.
DEBUG=false

# These three are read by docker-compose.prod.yml, not by the app directly.
# FRONTEND_URL becomes the backend's CORS_ORIGINS. BACKEND_URL and
# BACKEND_WS_URL are baked into the frontend at BUILD time as
# NEXT_PUBLIC_API_URL / NEXT_PUBLIC_WS_URL — changing them later needs a
# rebuild, not a restart. Setting CORS_ORIGINS directly does nothing: compose
# sets it explicitly from FRONTEND_URL and that wins.
FRONTEND_URL=https://app.synthiqvoice.com
BACKEND_URL=https://api.synthiqvoice.com
BACKEND_WS_URL=wss://api.synthiqvoice.com
```

Every one of these is passed into the backend container by
`docker-compose.prod.yml`. A variable set in Coolify that the compose file does
not list never reaches the app — which is why `SECRET_KEY`,
`CREDENTIAL_ENCRYPTION_KEYS` and `PUBLIC_URL` are declared required there: the
deploy fails with a message naming the missing variable rather than starting a
server that cannot take a call. `TELNYX_PUBLIC_KEY` is passed but not required,
since each workspace can hold its own.

#### Settings that changed behaviour in November 2026

Names and defaults are from `backend/app/core/config.py`.

| Setting | Default | What it does |
| --- | --- | --- |
| `SECRET_KEY` | none usable | Signs login tokens, call stream tokens and embedded widget sessions. The app refuses to start on an empty value or on any placeholder shipped in this repository, unless `DEBUG=true`. Every backend instance must share one value |
| `CREDENTIAL_ENCRYPTION_KEYS` | none | Encrypts the provider keys and integration credentials customers store. `<id>:<base64 32-byte key>`, comma separated: the first entry encrypts, every entry can decrypt. The app refuses to start without it unless `DEBUG=true`, and refuses a malformed value always. Every backend instance must share one value. Losing it loses every stored credential |
| `RETENTION_SWEEP_ENABLED` | `false` | Runs the sweep that deletes call data older than each account's retention period. Off unless set: an upgrade never starts deleting on its own. The first sweep after it is turned on is a rehearsal that only counts and logs what it would delete (`retention_sweep_would_delete`); deletion starts one interval later. An account that never set a period is not swept, and a period under 30 days is refused. Off, the compliance page reports retention as not in force |
| `RETENTION_SWEEP_INTERVAL_HOURS` | `24` | How often the sweep runs. One process at a time holds a Redis lock, so several workers do not sweep at once |
| `CALLER_AUDIO_DIR` | `./data/caller-audio` (`/app/data/caller-audio` in `docker-compose.prod.yml`) | Where the audio of caller test calls is stored: one WAV file per side of a call, in a folder per workspace, under names the server generates. **It must be a persistent volume.** The production compose file mounts the named volume `caller_audio` there. On Coolify keep that volume, or mount your own persistent storage at the same path; on the container's own disk the files are lost at each redeploy while the database still lists them, and the call page then reports the recording as gone. Back it up with the database: erasure and retention delete from it, but nothing else copies it |
| `CALLER_AUDIO_EXPIRY_INTERVAL_MINUTES` | `60` | How often expired call audio is deleted. Always on; also runs once at start-up. Minimum 1 |
| `CALLER_AUDIO_MAX_BYTES` | `67108864` (64 MB) | Largest single audio file accepted. A larger one is refused with 413 and nothing is stored. Only genuine WAV files are kept: the server checks the bytes, never the Content-Type |
| `PUBLIC_URL` | unset | The backend's public origin. Used for the media stream URL the carrier dials, for the webhook addresses given to carriers, and as the URL a Twilio signature is checked against. Required by the compose file |
| `ALLOW_UNSIGNED_WEBHOOKS` | `false` | `true` accepts carrier webhooks with no verified signature. For a local rehearsal with no carrier key only. It logs at critical on startup and on every webhook it admits, and the preflight fails on it |
| `FRONTEND_URL` | unset | Origin the embed snippet points other sites at. Falls back to the first CORS origin, then `PUBLIC_URL` |
| `EMBED_ALLOW_ANY_ORIGIN_WHEN_UNSET` | `false` | An agent with no allowed domains can be embedded by no other site. `true` restores the old meaning, every site, and logs a warning each time it is the reason a request was served |
| `EMBED_SESSION_MAX_SECONDS` | `900` | Longest an embedded session can run, and the lifetime of its signed token. The session is closed when it is reached |
| `EMBED_TOOLS_DEFAULT_ENABLED` | `false` | Whether embedded sessions may call tools when the agent does not say. The per-agent switch is `embed_settings.allow_tools`, set through the agent's embed settings API; the dashboard has no control for it yet |
| `TWILIO_AUTH_TOKEN` | unset | Fallback for Twilio signature checks. See below |
| `TELNYX_PUBLIC_KEY` | unset | Fallback for Telnyx signature checks, after the owning workspace's own key. See below |
| `RATE_LIMIT_STORAGE_URI` | unset | Where rate-limit counts are kept. Unset: Redis at `REDIS_URL`, so every worker shares one count. `memory://` counts per process, which is only right for a single process. While Redis is unreachable the same limits are counted per process until it returns |
| `CALL_MAX_DURATION_S` | `1800` | Longest a phone call may run, in seconds. Past it the bridge ends the call and records `max_duration`. Replaces a fixed 300 s cap. The model provider's own limit is 60 minutes |
| `CALL_IDLE_TIMEOUT_S` | `120` | Seconds of no conversation (nobody speaking, no agent audio, no tool running) before the bridge ends the call and records `silence_timeout`. `0` turns it off |
| `CALL_TOOL_GRACE_S` | `10` | When a call ends while a tool is running, how long the tool may finish before it is cancelled |

Three things follow that are not settings.

- **Redis is part of the call path.** Each call's stream token is claimed in
  Redis so it can be used once. While Redis is unreachable every carrier media
  stream is refused, by design: the log line is
  `stream_token_claim_store_unavailable`, and a replayed token logs as
  `replayed`. All backend instances must share one Redis.
- **Twilio webhooks are verified per workspace.** The signature is checked
  against the Twilio auth token of the workspace that owns the number, agent or
  call the webhook is about (Settings > Workspace API Keys), then against the
  global `TWILIO_AUTH_TOKEN` if one is set. With neither, the webhook is
  refused 403. Telnyx works the same way: the owning workspace's public key
  first, then the global `TELNYX_PUBLIC_KEY` if one is set.
- **Deploy the frontend with the backend.** The widget page must send the
  signed session the backend now requires; an older page gets no tools and no
  transcript.

### Credential encryption key

Provider keys and integration credentials are stored encrypted. Three things an
operator needs to know:

- **First deploy with this version.** Set `CREDENTIAL_ENCRYPTION_KEYS` before
  deploying. The migration then encrypts every credential already stored. Check
  with `uv run python scripts/rotate_credential_keys.py --check` from `backend/`;
  it lists how many stored values sit under each key and should show none as
  `plain`.
- **Rotating the key.** Put a new key first and keep the old one behind it
  (`k2:<new>,k1:<old>`), restart, run `scripts/rotate_credential_keys.py`, and
  only when it reports nothing left under `k1` remove `k1` and restart again.
  Removing a key while rows still name it makes those credentials unreadable.
- **Backups.** A database backup is useless for credentials without this key,
  and the key is useless without the backup. Store them apart.

The audit trail (`audit_events`) and the record of each export, erasure and
retention sweep (`compliance_runs`) are created by the same migration. Audit
events are also written to the log as JSON lines with `"logger": "audit"`,
whatever the application's log level.

`docker-compose.prod.yml` passes the backend every setting in the table above,
including `ALLOW_UNSIGNED_WEBHOOKS`, the three `EMBED_*` settings, the `TWILIO_*`
pair and `RATE_LIMIT_STORAGE_URI`. A setting that is not in the backend's
`environment:` block there keeps its default whatever is set in Coolify.

Migrations run themselves. `docker-entrypoint.sh` waits for Postgres and Redis,
runs `alembic upgrade head`, then starts gunicorn — there is no manual migration
step.

**The OpenAI key does not go here.** The call path reads it from the agent's
**workspace**, not the environment — Settings > Workspace API Keys in the
dashboard, after the first login. There is no fallback: a workspace without a
key raises "API key not configured for this workspace" the moment the caller is
connected. `make preflight` checks the workspace key, not the environment one,
for this reason.

```

### Step 5: Cloudflare DNS

Two A records to the server's IP:

| Record | Proxy | Why |
| --- | --- | --- |
| `app.synthiqvoice.com` | **Proxied** (orange) | Ordinary web traffic. Cloudflare's cache and WAF are worth having in front of the dashboard |
| `api.synthiqvoice.com` | **DNS only** (grey) | Telnyx posts webhooks here and opens the media stream here. Proxying it puts Cloudflare's edge between the carrier and the audio: it adds a hop to `time_to_first_audio`, which is one of the metrics this platform measures, and Free and Pro plans close a WebSocket after 100 seconds idle |

Set SSL/TLS mode to **Full (strict)** for the zone. Flexible would have
Cloudflare talk plain HTTP to the origin, which breaks the `app` → `api`
requests the dashboard makes.

Leave `api` grey while Coolify issues certificates in the next step: the
Let's Encrypt HTTP-01 challenge is answered by Traefik on the origin, and a
proxied record can intercept it. `app` can be turned orange once its certificate
is issued.

Grey-clouding `api` exposes the server's IP for that hostname. If that matters
later, restrict port 443 on the host firewall to Telnyx's published IP ranges
rather than putting the proxy back in front of the audio.

### Step 6: Configure Domains in Coolify

For each service that needs external access:

**Backend Service:**
1. Click on `backend` service
2. Go to **Domains** tab
3. Add: `api.synthiqvoice.com`
4. Port: `8000`
5. Enable **HTTPS** (Let's Encrypt)

**Frontend Service:**
1. Click on `frontend` service
2. Go to **Domains** tab
3. Add: `app.synthiqvoice.com`
4. Port: `3000`
5. Enable **HTTPS** (Let's Encrypt)

### Step 7: Deploy

1. Click **Deploy** button
2. Wait for build (~5-10 minutes first time)
3. Check logs for any errors

### Step 8: Verify Deployment

```bash
# Check backend health
curl https://api.synthiqvoice.com/health

# Should return: {"status": "healthy", ...}
```

Visit `https://app.synthiqvoice.com` - you should see the login page.

### Step 9: Wire the phone number, then preflight before dialling

1. Log in, and add the OpenAI key under **Settings > Workspace API Keys**. The
   call path reads it from there, not from the environment.
2. Create an agent with `phone_number_id` set to the number in E.164
   (`+15551234567`) and **is_active** on. An inactive agent closes the media
   stream with 4003.
3. In Telnyx, point the number at a **TeXML application** — not Call Control —
   with the voice webhook at `https://api.synthiqvoice.com/webhooks/telnyx/voice`
   and the status callback at `https://api.synthiqvoice.com/webhooks/telnyx/status`.
   Only TeXML consumes the document the webhook returns; on Call Control the
   document is ignored and the call goes nowhere.
4. Run the preflight from somewhere that can reach the database and Redis:

   ```bash
   make preflight ARGS="--url https://api.synthiqvoice.com"
   ```

   It checks the credentials, `PUBLIC_URL`, the migration head, Redis, the
   agent's number, the workspace OpenAI key, the public host, and then opens a
   real websocket to the media stream URL. It places no call and writes no row.
   Exit status is 0 only when every check passed. A `warn` is a judgement call;
   a `FAIL` is not.
5. Dial the number. Then check, in order: a `call_records` row with that
   `CallSid`; `turns` on it non-null; `GET /api/v1/calls/{id}/metrics` reporting
   a measured `time_to_first_audio`. `transcription_accuracy: null` is correct —
   a human caller has no script to score against.

`docs/HANDOFF.md` section 7 has the full version, including what each failure means.

---

## Post-Deployment Setup

### 1. Login & Initial Setup

1. Go to `https://app.synthiqvoice.com`
2. Login with your admin credentials
3. **Create a Workspace** (required for multi-tenant isolation)

### 2. Add API Keys

Go to **Settings** → **Workspace API Keys**:

**For OpenAI (Direct):**
- OpenAI API Key: `sk-...`
- Provider: OpenAI

**For Azure OpenAI:**
- Provider: Azure
- Azure Endpoint: `https://your-resource.openai.azure.com/`
- Azure API Key: `your-azure-key`
- Deployment Name: `gpt-4o-realtime` (or your deployment)

**For Telnyx:**
- Telnyx API Key: `KEY...`
- Telnyx Public Key: (optional, for webhook verification)

### 3. Configure Telnyx Webhooks

In **Telnyx Portal** → Your Phone Number → **Voice Settings**:

| Setting | Value |
|---------|-------|
| Connection Type | Webhook |
| Webhook URL | `https://api.synthiqvoice.com/webhooks/telnyx/voice` |
| Failover URL | (leave empty) |

For **TeXML Applications** (if using):
- Answer URL: `https://api.synthiqvoice.com/webhooks/telnyx/voice`
- Status URL: `https://api.synthiqvoice.com/webhooks/telnyx/status`

### 4. Create Your First Agent

1. **Dashboard** → **Agents** → **Create Agent**
2. Fill in:
   - Name: `Demo Agent`
   - System Prompt: Your agent's personality/instructions
   - Voice: Select a voice
   - Language: Select language
3. **Assign Phone Number** to the agent
4. Save

### 5. Test End-to-End

**Inbound Call Test:**
1. Call your Telnyx phone number
2. Agent should answer and respond

**Outbound Call Test:**
1. Dashboard → **Make Call** button
2. Enter a phone number
3. Select your agent
4. Click Call

---

## Troubleshooting

### Build Fails

**Check Coolify build logs:**
- Look for npm/pip errors
- Verify Dockerfile paths are correct

**Common fixes:**
```bash
# If node_modules issues
rm -rf frontend/node_modules frontend/.next
git add -A && git commit -m "Clean build" && git push
```

### Backend Won't Start

**Check logs:**
```bash
# In Coolify, click backend service → Logs
```

**Common issues:**
- Database not ready: Check postgres container health
- Missing env vars: Verify all required vars are set
- Migration failed: Check alembic output in logs

### Webhooks Not Working

1. **Verify domain is accessible:**
   ```bash
   curl https://api.synthiqvoice.com/health
   ```

2. **Check Telnyx webhook URL** is exactly:
   ```
   https://api.synthiqvoice.com/webhooks/telnyx/voice
   ```

3. **Check backend logs** for webhook errors

### WebSocket Connection Fails

- Ensure `wss://` URL is correct (not `ws://`)
- Verify Coolify proxy supports WebSocket (it does by default)
- Check SSL certificate is valid

### CORS Errors

- Verify `FRONTEND_URL` env var matches your actual frontend domain
- Include `https://` in the URL

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                 COOLIFY SERVER (Azure Germany)               │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                    Traefik Proxy                      │   │
│  │         (SSL termination, routing)                    │   │
│  └──────────────────────────────────────────────────────┘   │
│           │                           │                      │
│           ▼                           ▼                      │
│  ┌─────────────────┐        ┌─────────────────┐             │
│  │    Frontend     │        │     Backend     │             │
│  │   (Next.js)     │───────▶│   (FastAPI)     │             │
│  │   Port 3000     │        │   Port 8000     │             │
│  └─────────────────┘        └─────────────────┘             │
│                                     │                        │
│                    ┌────────────────┼────────────────┐      │
│                    ▼                ▼                ▼      │
│           ┌─────────────┐  ┌─────────────┐                  │
│           │  PostgreSQL │  │    Redis    │                  │
│           │   Port 5432 │  │  Port 6379  │                  │
│           └─────────────┘  └─────────────┘                  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │  Telnyx/Twilio  │
                    │   (Webhooks)    │
                    └─────────────────┘
```

---

## Quick Reference

### Call audio and how long it is kept

`synthiq-caller upload` sends the audio of each call after the run itself, one request per file. The audio is kept per workspace for `audio_retention_days` (30 unless the workspace changes it, from 1 to 3650; set it with `PUT /api/v1/workspaces/{id}` and `{"audio_retention_days": 90}`), counted from when the file was stored.

Expired audio is deleted **by the backend itself, with nothing to schedule or switch on**. Every backend process runs an expiry pass when it starts and then every `CALLER_AUDIO_EXPIRY_INTERVAL_MINUTES` (60 by default); on PostgreSQL an advisory lock keeps several workers from all doing it, and deleting an already-deleted file is harmless. A call page or audio request also expires that workspace's due audio first, so a recording past its period is never served in the gap between passes. The row is kept, marked expired, and the call page then says "Recording expired" rather than showing a player.

This is independent of `RETENTION_SWEEP_ENABLED`, which stays off by default and covers the other call data, not audio. `scripts/expire_caller_audio.py` still exists as an extra (a manual or cron pass, `--dry-run` to count; exits 1 if a file could not be deleted).

Deleting a run, replacing a run on upload, and erasing an account delete the audio at once, files first and then rows; a file that will not delete is reported (the run is kept, or the erasure is marked incomplete) rather than left behind unsaid. Account export lists each stored recording with the date it will be deleted after.

The call page plays audio through the API with the signed-in user's token: the browser cannot attach it to an audio element, so the page downloads each recording and plays it from memory. Long recordings are therefore downloaded whole before they play.

### Generate Secure Passwords

```python
# Run this to generate secrets
import secrets
print(f"SECRET_KEY={secrets.token_hex(32)}")
import base64
print(f"CREDENTIAL_ENCRYPTION_KEYS=k1:{base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}")
print(f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}")
print(f"REDIS_PASSWORD={secrets.token_urlsafe(24)}")
print(f"ADMIN_PASSWORD={secrets.token_urlsafe(16)}")
```

### Webhook URLs

| Webhook | URL |
|---------|-----|
| Telnyx Voice | `https://api.YOUR-DOMAIN/webhooks/telnyx/voice` |
| Telnyx Status | `https://api.YOUR-DOMAIN/webhooks/telnyx/status` |
| Telnyx Answer | `https://api.YOUR-DOMAIN/webhooks/telnyx/answer` |
| Twilio Voice | `https://api.YOUR-DOMAIN/webhooks/twilio/voice` |
| Twilio Status | `https://api.YOUR-DOMAIN/webhooks/twilio/status` |

### Health Check Endpoints

| Service | URL |
|---------|-----|
| Backend | `https://api.YOUR-DOMAIN/health` |
| API Docs | `https://api.YOUR-DOMAIN/docs` |
| Frontend | `https://YOUR-DOMAIN` |

---

## Support

If deployment fails:
1. Check Coolify logs for each service
2. Verify all environment variables are set
3. Ensure domains are properly configured with SSL
4. Test webhook URLs are accessible from internet

---

## Behaviour changes: tenant isolation and start-up checks (November 2026)

Read this before upgrading an existing deployment. Each item changes what a running instance does.

### The deployment will not start until these are set

`docker-compose.prod.yml` no longer has a default for any credential. Compose stops with a message naming the variable if one is missing:

- `POSTGRES_PASSWORD`
- `REDIS_PASSWORD`
- `ADMIN_EMAIL`
- `ADMIN_PASSWORD`

If your deployment was running on the old defaults, the database and Redis already hold those passwords. Change the password inside Postgres (`ALTER USER ... PASSWORD ...`) and set the same value in `POSTGRES_PASSWORD`; changing the variable alone does not change an existing database.

### The backend refuses passwords this repository has shipped

Outside `DEBUG=true` the backend stops at start-up when:

- the database or Redis password is one of the old defaults or example values;
- there are no users yet and `ADMIN_PASSWORD` is a shipped value;
- an administrator account still has a shipped password stored.

For the last case, set `ADMIN_EMAIL` to that account's address and `ADMIN_PASSWORD` to a new value, then start again. The stored password is replaced at start-up.

### Sign-up is closed

`POST /api/v1/auth/register` answers 403 unless `ALLOW_PUBLIC_SIGNUP=true`. The administrator creates accounts:

```bash
curl -X POST https://api.YOUR-DOMAIN/api/v1/auth/users \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"email": "clinic@example.com", "username": "Clinic", "password": "..."}'
```

To switch an account off or back on (it loses access on its next request, not when its token expires):

```bash
curl -X POST https://api.YOUR-DOMAIN/api/v1/auth/users/42/disable -H "Authorization: Bearer $ADMIN_TOKEN"
curl -X POST https://api.YOUR-DOMAIN/api/v1/auth/users/42/enable  -H "Authorization: Bearer $ADMIN_TOKEN"
```

### One owner per phone number

- The migration `d2f5a8c31b47` adds a unique index on live phone numbers. If two live records already hold the same number, the upgrade stops, changes nothing, and prints the record and owner ids that collide. Release or delete the wrong record and run it again.
- A second account that registers a number another account holds gets 409.
- An agent's phone field only accepts a number registered to the same account. A carrier id from the agent page's number list is still accepted.
- An inbound call reaches only an agent in the account that holds the number.

### Requests that now answer 404 or 403

- Another account's agent id on the tool, realtime token, realtime session, transcript and phone-number assignment routes: 404.
- Another account's workspace or agent when creating a campaign: 404.
- A tool the agent's owner did not enable, on `POST /api/v1/tools/execute` and on phone calls: refused.

### Operational endpoints

- `/docs`, `/redoc` and `/openapi.json` answer 404 unless `ENABLE_API_DOCS=true`.
- `/metrics` answers 404 unless `METRICS_TOKEN` is set and the scraper sends it as `Authorization: Bearer <token>`.
- Ports 8000 and 3000 are no longer published on the host. The reverse proxy reaches both services over the compose network. Anything that called `http://SERVER-IP:8000` directly must use the public address.
- The backend container runs as user `app` (uid 10001), not root.

### Outbound requests and webhooks

- The QA alert webhook, the Slack alert webhook and the Azure OpenAI endpoint must be `https://` and must resolve to public addresses. A private destination your own operator needs (an Azure private endpoint, for example) goes in `OUTBOUND_ALLOW_PRIVATE_HOSTS` as a JSON list of host names.
- A Shopify shop domain must be `<name>.myshopify.com`.
- A Telnyx webhook whose timestamp is more than five minutes from the server's clock is refused (`TELNYX_WEBHOOK_MAX_AGE_SECONDS`). Keep the server's clock synchronised.

### New settings

| Setting | Default | Meaning |
|---------|---------|---------|
| `ALLOW_PUBLIC_SIGNUP` | follows `DEBUG` | Whether anyone may create an account |
| `ENABLE_API_DOCS` | follows `DEBUG` | Serve the interactive API documentation |
| `METRICS_TOKEN` | unset | Bearer token required to read `/metrics` |
| `TELNYX_WEBHOOK_MAX_AGE_SECONDS` | `300` | Oldest signed Telnyx webhook accepted |
| `OUTBOUND_ALLOW_PRIVATE_HOSTS` | `[]` | Host names the server may call although they are private |
