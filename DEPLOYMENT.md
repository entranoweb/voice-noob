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
POSTGRES_DB=voicenoob

# Redis (REQUIRED - change this!)
REDIS_PASSWORD=YourSecureRedisPassword456!

# Security (REQUIRED - generate a new one!)
# Generate: python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY=your_64_character_hex_string_here

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
# Unset, verification falls back to DEBUG: with DEBUG=false every webhook is
# rejected 403 and no call ever reaches an agent; with DEBUG=true every webhook
# is accepted unverified by anyone who finds the URL.
TELNYX_PUBLIC_KEY=your_telnyx_public_key

# Only needed for outbound calls and buying numbers. Inbound does not use it.
TELNYX_API_KEY=your_telnyx_api_key

# REQUIRED in production. See TELNYX_PUBLIC_KEY above.
DEBUG=false

# Where the dashboard is served from, so the browser can call the API.
CORS_ORIGINS=https://app.synthiqvoice.com
```

**The OpenAI key does not go here.** The call path reads it from the agent's
**workspace**, not the environment — Settings > Workspace API Keys in the
dashboard, after the first login. There is no fallback: a workspace without a
key raises "API key not configured for this workspace" the moment the caller is
connected. `make preflight` checks the workspace key, not the environment one,
for this reason.

```

### Step 5: Configure Domains in Coolify

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

### Step 6: Deploy

1. Click **Deploy** button
2. Wait for build (~5-10 minutes first time)
3. Check logs for any errors

### Step 7: Verify Deployment

```bash
# Check backend health
curl https://api.synthiqvoice.com/health

# Should return: {"status": "healthy", ...}
```

Visit `https://app.synthiqvoice.com` - you should see the login page.

### Step 8: Wire the phone number, then preflight before dialling

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
