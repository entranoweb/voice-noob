# Synthiq Voice

Two things live in this repository:

1. **A test caller for voice agents** (`caller/`). It plays recorded caller audio into a live voice agent, reacts to what the agent says, and grades the call from the agent's own tool calls and the record it stored. It runs from the command line.
2. **A voice agent platform** (`backend/`, `frontend/`). A dashboard and API for building phone agents on OpenAI's speech-to-speech model, with Telnyx or Twilio for the phone line.

They are not joined yet: the dashboard cannot start or show a caller run.

Forked from [voice-noob](https://github.com/KenKaiii/voice-noob) by Ken Kai.

## What works today

Read this before relying on anything. The detail is in `docs/HANDOFF.md` and `docs/ROADMAP-2027.md`.

| Part | State |
| --- | --- |
| Caller over a direct audio connection to an ElevenLabs agent | Works; used on real test calls |
| Caller over a phone line | Built and tested against a stand-in; no real call placed yet |
| Caller trace adapters for Retell and Vapi | Written from the vendors' documentation; never run against a real account |
| Platform: inbound and outbound phone calls | Built and tested; never run on a deployment |
| Platform: text test harness (the QA page) | Works when switched on by the server operator. It simulates the agent's prompt on a text model, not the speech model that answers calls |
| Hosted deployment | None |

## Quick start

### You need

- [uv](https://docs.astral.sh/uv/) (it installs the right Python for you)
- Node.js 22
- Docker with Compose v2
- `make`, and a POSIX shell. On Windows use Git Bash or WSL

### First run

```bash
git clone https://github.com/entranoweb/voice-noob.git
cd voice-noob
make setup
```

`make setup` does five things, in this order:

1. Writes `backend/.env` and `frontend/.env`. The backend file gets a generated signing key and a generated admin password, which it prints once. Do not copy `backend/.env.example` by hand: the app refuses to start on its placeholder key.
2. Installs the backend, caller and frontend dependencies.
3. Starts Postgres and Redis in Docker and waits until both are healthy.
4. Applies the database migrations.
5. Prints the two commands below.

Then, in two terminals:

```bash
cd backend && uv run uvicorn app.main:app --reload
```

```bash
cd frontend && npm run dev
```

Open <http://localhost:3000> and sign in as `admin@example.com` with the password `make setup` printed. It is also in `backend/.env` as `ADMIN_PASSWORD`. That account is created the first time the backend starts on an empty database.

### Before an agent can take a call

- Create a workspace, then save your OpenAI key in **Settings** with that workspace selected. A call reads its keys from the agent's workspace only.
- A real phone call also needs a public address and carrier settings. See `DEPLOYMENT.md`.

### If something is off

- **Port 5432 or 6379 is already in use.** Stop the other Postgres or Redis, or change the port mapping in `docker-compose.yml` and the matching URL in `backend/.env`.
- **You ran an older version of this repository.** The local database is now called `voicenoob`. An old Docker volume still holds the previous name: run `make clean`, then `make setup`. This deletes local data.
- **You want a fresh admin password.** Delete `backend/.env` and run `make setup` again, on an empty database.

## Tests

```bash
make test            # backend, frontend and caller
make test-backend    # backend only
make test-frontend   # frontend only
make test-caller     # caller only
```

The backend tests run on Postgres, as CI does. `make test-backend` starts the Postgres container and creates a `voicenoob_test` database if it is missing. Run without Postgres, the suite falls back to SQLite and fails tests that are correct.

Other checks:

```bash
make lint            # ruff, mypy, eslint
make check           # everything CI runs for the backend and frontend
```

## The caller

`caller/` is its own package with its own lock file and no dependency on the backend. Start with `caller/README.md`. In short:

```bash
cd caller
uv sync --all-extras
uv run synthiq-caller --help
```

## Layout

```text
backend/     FastAPI app: API, call bridge, text test harness, migrations
frontend/    Next.js dashboard and the embeddable widget
caller/      Test caller: legs, scenarios, grading, batch runner, reports
docs/        Handoff notes, roadmap, decisions
integrations/ promptfoo provider for the text harness
```

## Stack

- **Calls:** OpenAI Realtime speech-to-speech. It is the only engine the call path uses. There is no separate speech-recognition, text-model and speech-synthesis pipeline yet, and keys for other providers can be stored but are not used on a call.
- **Backend:** FastAPI, PostgreSQL 17, Redis 7, SQLAlchemy 2
- **Frontend:** Next.js 16, React 19, TypeScript, Tailwind, shadcn/ui
- **Phone line:** Telnyx or Twilio

## More

- `DEPLOYMENT.md`: running it for real, and every setting
- `docs/HANDOFF.md`: what has and has not been proven
- `docs/ROADMAP-2027.md`: where it is going
- `caller/README.md` and `caller/docs/`: the test caller

## License

AGPL-3.0
