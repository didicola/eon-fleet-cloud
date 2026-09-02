# EON Fleet Cloud

24/7 autonomous ghost fleet pipeline running on **GitHub Actions** (GitHub's cloud),
so the fleet stays alive even when the local device is off.

## What it does
- **Every 10 minutes** (GitHub Actions cron), health-checks every deployed ghost worker
- Pushes the live registry to the permanent dashboard `https://ghost.eon-sovereign.workers.dev`
- Broadcasts fleet status to Telegram `@eon_fleet`
- Commits the updated registry back to this repo

## Architecture
```
GitHub Actions (24/7 cloud)
    │  cron */10
    ▼
fleet-cloud.py ──► health-check each round
    │
    ├──► push registry → ghost.eon-sovereign.workers.dev/update
    ├──► Telegram broadcast → @eon_fleet
    └──► commit registry → this repo
```

The ghost workers themselves run on **Cloudflare Workers edge** (24/7, always-on).
This pipeline keeps them discoverable and healthy without needing the laptop.

## Secrets (GitHub repo → Settings → Secrets)
| Secret | Value |
|--------|-------|
| `EON_DASHBOARD_URL` | `https://ghost.eon-sovereign.workers.dev` |
| `EON_DASHBOARD_SECRET` | the ghost dashboard secret |
| `EON_TG_BOT` | Telegram bot token |
| `EON_TG_CHANNEL` | `@eon_fleet` |
| `EON_CF_TOKEN` | Cloudflare account token (for minting) |
| `EON_CF_ACCOUNT` | Cloudflare account ID |

## Manual run
Trigger the workflow from the Actions tab, or:
```bash
gh workflow run fleet-pipeline.yml
```