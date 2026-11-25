# Release flow: main, staging, production

This repository follows the BusTrack branching model (project document `overview/git-branching-model.md`).

| Branch / tag | Role | Publishes? |
|---|---|---|
| `main` | default and integration branch; PRs with `security-check` | **no** |
| `staging` | pointer, fast-forward only | push touching `src/**`, `requirements.txt` or `schedule_update.yml` → `uta-beta`, `pvta-beta` |
| `production` | pointer, fast-forward only | same paths → `uta`, `pvta`; the monthly cron builds `production` code |
| `prod-YYYYMMDD-HHMM` | tag on every production promotion | no |

Release tag names are a consumer contract (iOS) and unchanged.

## Promote

Actions → **Promote** → *Run workflow* on `main`:

```sh
R=gorkemg/bustrack-schedules
gh workflow run promote.yml -R $R --ref main -f target=staging      # main HEAD -> staging
gh workflow run promote.yml -R $R --ref main -f target=production   # staging HEAD -> production (owner only)
```

Production must equal the current `staging` HEAD. The pointer moves with the `PROMOTE_SSH_KEY` deploy key (secret in the environments `staging` and `production`). To republish without a code change: `gh workflow run schedule_update.yml -R $R --ref main -f channel=production`.

## Roll back

**Roll back production pointer** (`rollback-pointer.yml`) with a `prod-*` tag, owner only; then dispatch *Update Transit Schedules* (channel production) to republish, and fix forward on `main`.

## Rules for agents

Merge into `main` where allowed (it publishes nothing). Promote `staging` only when the owner asked for a beta test. Never move `production`.
