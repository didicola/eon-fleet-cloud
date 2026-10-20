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
| `EON_CF_SESSION` | JSON session pool exported from the box's Firefox mother profiles (`scripts/export-cf-session.py`); enables the cloud claim |

## Claim lane (cloud, no local box)
The ubuntu lane's claim step is `eon-matrix-renew.sh` → rotates logged-in Firefox
"mother" profiles (`~/.config/eon/mothers/*`) → `matrix-claim.py` over Marionette →
clicks **Claim** in the Cloudflare dashboard. GitHub's bare headless Chromium is
stopped by Cloudflare's security check — that is why the registry reported
`0 persistent`.

The cloud port (`scripts/fleet-claim-session.py`) opens each fresh claim URL in
Playwright Firefox with the mother's session cookies, on the same WARP egress
class the box uses. To enable it:

1. On the box, export the sessions (JSON credential — never commit it):
   ```bash
   curl -fsSL https://raw.githubusercontent.com/didicola/eon-fleet-cloud/main/scripts/export-cf-session.py \
     | python3 > ~/cf_session.json
   chmod 600 ~/cf_session.json
   ```
2. Repo → Settings → Secrets and variables → Actions → New repository secret
   `EON_CF_SESSION` = contents of `cf_session.json`.
3. The next run claims each fresh mint and marks `claim_status: claimed` in
   `scripts/cloud_rounds.json`; `fleet-cloud.py` then counts it persistent.

If the export contains no Cloudflare cookies, log in to `dash.cloudflare.com`
once in that Firefox profile first. The workflow never prints the session,
claim tokens, or account emails.

## Manual run
Trigger the workflow from the Actions tab, or:
```bash
gh workflow run fleet-pipeline.yml
```

---
**Mission-control link (2026-09-27):** consumed by [eon-p2p-cloud](https://github.com/didicola/eon-p2p-cloud) (`FLEET-LINK.md`, D2573). Mint/claim stay here; monitors stay there.
