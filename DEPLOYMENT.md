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

# Telnyx webhook signature verification. REQUIRED.
# Unset, every webhook is refused 403 and no call ever reaches an agent,
# whatever DEBUG says. The only way to accept an unsigned webhook is
# ALLOW_UNSIGNED_WEBHOOKS=true, which must never be set here.
TELNYX_PUBLIC_KEY=your_telnyx_public_key

# Only needed for outbound calls and buying numbers. Inbound does not use it.
TELNYX_API_KEY=your_telnyx_api_key

# REQUIRED in production. See TELNYX_PUBLIC_KEY above.
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
not list never reaches the app — which is why `PUBLIC_URL` and
`TELNYX_PUBLIC_KEY` are declared required there: the deploy fails with a message
naming the missing variable rather than starting a server that cannot take a
call.

#### Settings that changed behaviour in November 2026

Names and defaults are from `backend/app/core/config.py`.

| Setting | Default | What it does |
| --- | --- | --- |
| `SECRET_KEY` | none usable | Signs login tokens, call stream tokens and embedded widget sessions. The app refuses to start on an empty value or on any placeholder shipped in this repository, unless `DEBUG=true`. Every backend instance must share one value |
| `CREDENTIAL_ENCRYPTION_KEYS` | none | Encrypts the provider keys and integration credentials customers store. `<id>:<base64 32-byte key>`, comma separated: the first entry encrypts, every entry can decrypt. The app refuses to start without it unless `DEBUG=true`, and refuses a malformed value always. Every backend instance must share one value. Losing it loses every stored credential |
| `RETENTION_SWEEP_ENABLED` | `false` | Runs the sweep that deletes call data older than each account's retention period. Off unless set: an upgrade never starts deleting on its own. The first sweep after it is turned on is a rehearsal that only counts and logs what it would delete (`retention_sweep_would_delete`); deletion starts one interval later. An account that never set a period is not swept, and a period under 30 days is refused. Off, the compliance page reports retention as not in force |
| `RETENTION_SWEEP_INTERVAL_HOURS` | `24` | How often the sweep runs. One process at a time holds a Redis lock, so several workers do not sweep at once |
| `PUBLIC_URL` | unset | The backend's public origin. Used for the media stream URL the carrier dials, for the webhook addresses given to carriers, and as the URL a Twilio signature is checked against. Required by the compose file |
| `ALLOW_UNSIGNED_WEBHOOKS` | `false` | `true` accepts carrier webhooks with no verified signature. For a local rehearsal with no carrier key only. It logs at critical on startup and on every webhook it admits, and the preflight fails on it |
| `FRONTEND_URL` | unset | Origin the embed snippet points other sites at. Falls back to the first CORS origin, then `PUBLIC_URL` |
| `EMBED_ALLOW_ANY_ORIGIN_WHEN_UNSET` | `false` | An agent with no allowed domains can be embedded by no other site. `true` restores the old meaning, every site, and logs a warning each time it is the reason a request was served |
| `EMBED_SESSION_MAX_SECONDS` | `900` | Longest an embedded session can run, and the lifetime of its signed token. The session is closed when it is reached |
| `EMBED_TOOLS_DEFAULT_ENABLED` | `false` | Whether embedded sessions may call tools when the agent does not say. The per-agent switch is `embed_settings.allow_tools`, set through the agent's embed settings API; the dashboard has no control for it yet |
| `TWILIO_AUTH_TOKEN` | unset | Fallback for Twilio signature checks. See below |

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
  refused 403. Telnyx still uses the one global `TELNYX_PUBLIC_KEY`.
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

`docker-compose.prod.yml` passes the backend `SECRET_KEY`, `PUBLIC_URL`,
`FRONTEND_URL`, `TELNYX_PUBLIC_KEY`, `TELNYX_API_KEY` and `DEBUG` from this
table's neighbourhood. It does **not** pass `ALLOW_UNSIGNED_WEBHOOKS`, the three
`EMBED_*` settings or the `TWILIO_*` pair, so on that deployment they keep their
defaults whatever is set in Coolify, until they are added to the backend's
`environment:` block.

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
