# Git Branch Strategy

## Branch Overview

| Branch | Purpose | Updates From |
|--------|---------|--------------|
| `main` | Upstream sync | KenKaiii/voice-noob (auto via GitHub Actions) |
| `synthiqvoice` | Development & new features | Auto-synced from `main` via GitHub Actions |
| `deploysynthiqvoice` | Production deployment | Manual merges from `synthiqvoice` |
| `backup/*` | Auto-created backups | Created before each sync attempt |

## Remotes

- **origin**: `https://github.com/entranoweb/voice-noob.git` (our fork)
- **upstream**: `https://github.com/KenKaiii/voice-noob.git` (original repo)

## Automated Sync Workflow

### How It Works
The GitHub Actions workflow (`sync-upstream.yml`) runs **daily at 00:00 UTC** and:

1. **Syncs `main` branch**:
   - Fetches from `upstream/main`
   - Creates backup branch (`backup/main-YYYYMMDD-HHMMSS`)
   - Attempts merge → pushes if clean, creates PR if conflicts

2. **Syncs `synthiqvoice` branch** (if main was updated):
   - Creates backup branch (`backup/synthiqvoice-YYYYMMDD-HHMMSS`)
   - Merges `main` into `synthiqvoice`
   - Pushes if clean, creates PR if conflicts

### Flow Diagram
```
upstream/main
      │
      ▼ (daily auto-sync)
    main ──────────────────► backup/main-*
      │
      ▼ (auto-sync after main updates)
synthiqvoice ──────────────► backup/synthiqvoice-*
      │
      ▼ (manual deploy)
deploysynthiqvoice
```

### Manual Trigger
You can manually trigger the sync from GitHub Actions:
- Go to Actions → "Sync from Upstream" → Run workflow
- Options: sync dev branch (default: true), create backups (default: true)

## Conflict Resolution

When conflicts occur, the workflow creates a PR instead of force-merging:

1. **Check the PR** - Lists conflicting files
2. **Checkout locally**:
   ```bash
   git fetch origin
   git checkout sync-main-to-dev-XXXXXX
   ```
3. **Resolve conflicts** - Keep our Azure OpenAI customizations
4. **Push and merge the PR**

### Restoring from Backup
If something goes wrong:
```bash
# List backup branches
git fetch origin
git branch -r | grep backup/

# Restore synthiqvoice from backup
git checkout synthiqvoice
git reset --hard origin/backup/synthiqvoice-YYYYMMDD-HHMMSS
git push origin synthiqvoice --force
```

## Manual Operations

### Deploying to Production
```bash
git checkout deploysynthiqvoice
git merge synthiqvoice
git push origin deploysynthiqvoice
```

### Force Sync Dev Branch (if needed)
```bash
git checkout synthiqvoice
git fetch origin main
git merge origin/main
# Resolve conflicts if any
git push origin synthiqvoice
```

## Custom Files (Watch for Conflicts)
These files contain our customizations and may conflict with upstream:
- `backend/app/models/user_settings.py`
- `backend/app/api/settings.py`
- `backend/app/services/gpt_realtime.py`
- `frontend/src/app/dashboard/settings/page.tsx`
- `frontend/src/lib/api/settings.ts`
- `backend/migrations/versions/015_add_azure_openai_fields.py`

## Security
- Next.js patched to 15.5.9 (CVE fix for DoS + source-leak) - Dec 2024
