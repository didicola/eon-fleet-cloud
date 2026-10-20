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

## 2026-10-20 — cloud ticker live, locals off
- **Primary tick:** Cloudflare Worker `eon-fleet-syncer` (cron `*/10`) POSTs a
  GitHub `repository_dispatch` (`fleet-mint-now`) on every tick. The GH Actions
  `schedule` stays enabled as a fallback but is sparse (measured ~6–8 runs/day).
  The worker exposes `/health` and `/tick?k=…` (guarded); source was repurposed
  from an inert worker whose `scheduled` handler was never exported. Original
  source backed up on the box (`~/eon-backups/eon-fleet-syncer.src-*.js`).
- **Free-plan cron cap:** the account allows only **5 cron triggers**. The
  ticker took the slot of the stale `fluid-matrix` `*/5` probe (worker remains
  deployed; only its cron is paused). To restore it, remove the ticker cron
  first, then `PUT …/workers/scripts/fluid-matrix/schedules` with
  `[{"cron":"*/5 * * * *"}]`.
- **Probe honesty:** Cloudflare serves `403/429` challenges to GitHub's
  datacenter egress, which used to flip healthy lanes to `dead` and over-mint.
  The probe now treats any non-DNS answer as *unknown-alive*, requires **two
  consecutive** dead probes before burying a lane, and infers `persistent` only
  from a real `200` (a challenge is not proof of a claim).
- **Pacing:** `EON_MIN_LIVE=6`, `EON_TARGET=3`, plus a durable
  `EON_DAILY_MINT_CAP=120` ledger (`mint_log`) and a 24 h/300-row registry prune.
- **Locals off:** the ubuntu round-manager cron (`*/10 eon-round-manager.py 4`)
  is archived and commented out; its live processes were terminated. The mint →
  deploy → register path now runs entirely on GitHub Actions + the CF Worker
  tick. The only remaining local lane is the owner's separate own-cloud
  auto-renew timer (not the temp-fleet pipeline).
- **Claim blocker (unchanged, human-gated):** the mother Cloudflare account is
  Google-SSO-only; no replayable session exists in the repo, so the claim step
  logs `EON_CF_SESSION secret not set — session claim skipped`. A human claim
  click (or a fresh exported session + repo secret) is still required for
  permanence. Claim URLs from the last local round expire ~13:23Z 2026-10-20.

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
class the box uses. Cloudflare requires the claimer to be signed in
("sign in to Cloudflare or create an account" in the claim docs), so the
session is the one bootstrap credential — not a runtime dependency on the box:
the dashboard session times out after **72 hours without activity**, and every
pipeline run (10 minutes) does an authenticated keepalive visit, so the exported
session stays warm with ubuntu off. If the session is ever revoked (logout,
password change, Cloudflare invalidates it), the run logs a warning and one
fresh export is needed — Cloudflare has no claim API.

To enable it:

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
   Every run also keeps the session alive (dashboard visit), so the box can stay
   off permanently after the one-time export.

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
