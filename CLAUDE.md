# Synthiq Voice

AI-powered voice agent platform for configuring and deploying custom voice agents with tool calling, multi-provider support, and transparent pricing tiers.

## Project Structure

```
voice-noob/
├── backend/                    # FastAPI Python backend
│   ├── app/
│   │   ├── api/               # API routes (agents, auth, crm, realtime, telephony, workspaces)
│   │   ├── core/              # Config, security, auth, rate limiting
│   │   ├── db/                # Database session, Redis client
│   │   ├── middleware/        # Request tracing, security headers
│   │   ├── models/            # SQLAlchemy models (user, agent, contact, appointment, workspace)
│   │   └── services/          # Business logic & integrations
│   │       └── tools/         # Voice agent tools (CRM, SMS, calendars)
│   ├── migrations/versions/   # Alembic database migrations
│   └── tests/                 # Backend tests (unit, integration, api)
├── frontend/                   # Next.js 15 React frontend
│   ├── src/
│   │   ├── app/dashboard/     # Dashboard pages (agents, crm, calls, settings, workspaces)
│   │   ├── app/embed/         # Embeddable voice widget
│   │   ├── components/ui/     # shadcn/ui components
│   │   ├── hooks/             # Custom React hooks
│   │   └── lib/api/           # API client functions
│   └── public/                # Static assets
└── docker-compose.yml         # PostgreSQL 17 + Redis 7
```

## Organization Rules

**Backend:**
- API routes → `app/api/`, one file per resource
- Business logic → `app/services/`, organized by domain
- Models → `app/models/`, one model per file
- Tools → `app/services/tools/`, one class per integration

**Frontend:**
- Pages → `src/app/dashboard/`, using Next.js App Router
- Components → `src/components/`, reusable UI elements
- Lib → `src/lib/`, utilities, types, API clients
- One component per file, co-locate related files

## Code Quality - Zero Tolerance

### Backend:
```bash
cd backend
uv sync --all-extras --frozen            # What CI installs. mypy needs the type
                                         # stubs in the extras: without them it
                                         # reports 30 errors that are not real
uv run ruff check app tests --fix        # Lint + auto-fix
uv run ruff format app tests             # Format
uv run mypy app                          # Type check (strict)
```

### Frontend:
```bash
cd frontend
npm run check                            # eslint + tsc + prettier
npm run lint:fix && npm run format       # Auto-fix
```

### Server Checks:
```bash
cd backend && uv run uvicorn app.main:app --reload   # Check runtime warnings
cd frontend && npm run dev                            # Check compilation warnings
```

**Fix ALL errors/warnings before continuing!**

## Agent Skills

`.claude/skills/` holds five Telnyx reference skills, pinned by content hash in
`skills-lock.json`:

| Skill | Covers |
| --- | --- |
| `telnyx-texml-python` | TeXML applications, calls, conferences, streams |
| `telnyx-voice-python` | Call Control — the outbound path (`telnyx.Call.create`) |
| `telnyx-voice-streaming-python` | Media streaming, including the bidirectional stream parameters |
| `telnyx-numbers-python` | Number search and orders (`/number_orders`) |
| `telnyx-numbers-config-python` | Per-number configuration and webhooks |

Update with `npx skills add team-telnyx/skills --skill <name> --agent claude-code`.

Two things they are not. They document the **REST APIs**, not the TeXML markup
verbs: the `<Stream bidirectionalMode="rtp">` attribute that silences a call
when it defaults to `mp3` is not in any of them, though the equivalent Call
Control enum is. And no skill covers OpenAI Realtime, which is where the session
shape that silenced the bridge came from — the registry has nothing for it,
FastAPI, SQLAlchemy or Next.js either. Skills shorten writing integration code;
they do not check that this codebase's own pieces agree with each other.

SMS is Twilio here, not Telnyx, so no messaging skill is installed.

## Key Commands

- `/update-app` - Update dependencies, fix deprecations
- `/check` - Run all quality checks, auto-fix issues
- `/commit` - Run checks, commit with AI message, push

## Tech Stack

**Voice & AI**: OpenAI Realtime (`gpt-realtime-2025-08-28`), speech-to-speech.
One engine — `telephony_ws.py` writes `engine="openai_realtime"` as a literal.
No cascaded STT/LLM/TTS pipeline exists; Deepgram and ElevenLabs are declared
dependencies whose clients are never constructed. Pipecat was removed in
`dd186f0` as an unused dependency — see `docs/COMPETITIVE_LANDSCAPE.md` before
reintroducing it
**Backend**: FastAPI, PostgreSQL 17, Redis 7, SQLAlchemy 2.0, Python 3.12+, uv
**Frontend**: Next.js 15, React 19, TypeScript 5.7, Tailwind, shadcn/ui
**Telephony**: Telnyx (primary), Twilio (optional)
