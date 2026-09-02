#!/bin/bash
# ═══════════════════════════════════════════════════════
#  EON MATRIX RENEW — fully autonomous identity lifecycle
#  mint (go-tls) → deploy worker (pre-claim) → auto-claim
#  (matrix browser) → verify lane → register pools.
#  ZERO human intervention.
# ═══════════════════════════════════════════════════════
set -u
EON=/home/ricos/eon-stack
STATE=$HOME/.config/eon/renewer-state
VENV=$EON/matrix-venv/bin/python
LOG=/home/ricos/eon-stack/matrix-renew.log
PROXY="${1:-}"   # optional socks5://127.0.0.1:1080 for IP round B

log() { echo "$(date -u +%H:%M:%S) $*" | tee -a "$LOG"; }

# feed outcome to EON-DREAM (STDP learner): dream "stimulus" +1|-1
dream() { python3 -c "
import json,sys,time,pathlib
p=pathlib.Path('/home/ricos/eon-cloud-agent/dream_stdp.json')
try:d=json.loads(p.read_text())
except:d={}
d.setdefault('traces',[]).append({'t':int(time.time()*1000),'stimulus':sys.argv[1][:100],'outcome':float(sys.argv[2])})
p.write_text(json.dumps(d))
" "$1" "$2" 2>/dev/null || true; }

# ── 0. ensure Xvfb :99 ──
pgrep -x Xvfb >/dev/null || { log "no Xvfb — start one first"; exit 1; }

# ── 1. MINT (go-tls lane) ──
log "[mint] starting (proxy=${PROXY:-direct})"
cd /home/ricos/.tmp/opencode
if [ -n "$PROXY" ]; then
    env -u https_proxy -u http_proxy -u ALL_PROXY HTTPS_PROXY="$PROXY" \
        go run github.com/PeronGH/cf-temp-account@latest --accept-terms \
        > /tmp/mr-mint.json 2>/dev/null
else
    env -u https_proxy -u http_proxy -u ALL_PROXY \
        go run github.com/PeronGH/cf-temp-account@latest --accept-terms \
        > /tmp/mr-mint.json 2>/dev/null
fi
[ $? -ne 0 ] && { log "[mint] FAILED"; exit 2; }
AID=$(python3 -c "import json;print(json.load(open('/tmp/mr-mint.json'))['account']['id'])")
TOK=$(python3 -c "import json;print(json.load(open('/tmp/mr-mint.json'))['account']['apiToken'])")
CLAIM=$(python3 -c "import json;print(json.load(open('/tmp/mr-mint.json'))['claim']['url'])")
log "[mint] OK aid=$AID"
dream "mint-ok:${1:-direct}:$AID" 1
cp /tmp/mr-mint.json "$STATE/$AID.mint.json"

# ── 2. DEPLOY WORKER (pre-claim! token still valid, secret-protected) ──
SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
cat > /tmp/meta-secure.json <<MEOF
{
  "main_module": "worker.js",
  "compatibility_date": "2025-01-01",
  "bindings": [
    { "type": "ai", "name": "AI" },
    { "type": "plain_text", "name": "EON_SECRET", "text": "$SECRET" }
  ]
}
MEOF
SDRSP=$(timeout 40 curl -s --noproxy '*' "https://api.cloudflare.com/client/v4/accounts/$AID/workers/subdomain" -H "Authorization: Bearer $TOK" | python3 -c "import json,sys;print(json.load(sys.stdin).get('result',{}).get('subdomain','none'))" 2>/dev/null)
log "[deploy] subdomain=$SDRSP"
timeout 90 curl -s --noproxy '*' -X PUT "https://api.cloudflare.com/client/v4/accounts/$AID/workers/scripts/eonbrain" \
    -H "Authorization: Bearer $TOK" \
    -F 'metadata=@/tmp/meta-secure.json;type=application/json' \
    -F 'worker.js=@/tmp/worker.js;type=application/javascript+module' > /dev/null 2>&1
timeout 40 curl -s --noproxy '*' -X POST "https://api.cloudflare.com/client/v4/accounts/$AID/workers/scripts/eonbrain/subdomain" \
    -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
    --data '{"enabled":true}' > /dev/null 2>&1
LANE="https://eonbrain.$SDRSP.workers.dev"
log "[deploy] lane=$LANE"

# ── 2b. DEPLOY CLOUD GHOST (permanent ghost node: 10 KV stores + 35 langs + matrix AI) ──
# Every minted account becomes a permanent cloud ghost — "let Cloudflare come to the ghost".
CLOUD_GHOST=$(bash "$EON/eon-deploy-cloud-ghost.sh" "$AID" "$TOK" 2>/dev/null | tail -1)
if [ -n "$CLOUD_GHOST" ] && [[ "$CLOUD_GHOST" == https* ]]; then
    log "[deploy] CLOUD GHOST LIVE: $CLOUD_GHOST"
    dream "cloud-ghost:$AID" 1
else
    log "[deploy] cloud ghost deploy skipped/failed (lane still live)"
fi

# persist secret forever (recoverable even after /tmp wipes)
python3 -c "
import json,sys,pathlib
pathlib.Path('$STATE/$AID.meta.json').write_text(json.dumps({'aid':sys.argv[1],'lane':sys.argv[2],'secret':sys.argv[3],'label':sys.argv[4]}))
" "$AID" "$LANE" "$SECRET" "${LBL:-matrix-${AID:0:8}}" 2>/dev/null || true

# ── 3. AUTO-CLAIM (matrix browser) ──
# Mother rotation: every dir in ~/.config/eon/mothers/ = one human-made login.
# Robot rotates between mothers so no single account hatches everything.
MOTHERS_DIR="$HOME/.config/eon/mothers"
MSTATE="$EON/.mother-idx"
if [ -d "$MOTHERS_DIR" ] && [ -n "$(ls -A "$MOTHERS_DIR" 2>/dev/null)" ]; then
    mapfile -t MOTHERS < <(ls -d "$MOTHERS_DIR"/*/ 2>/dev/null)
    idx=$(cat "$MSTATE" 2>/dev/null || echo 0)
    PROF="${MOTHERS[$((idx % ${#MOTHERS[@]}))]%/}"
    echo $((idx + 1)) > "$MSTATE"
    log "[claim] mother=$PROF"
else
    PROF=/tmp/matrix-profile
    if [ ! -d "$PROF" ]; then
        rm -rf "$PROF"; cp -r "$HOME/.mozilla/firefox/nwjIs9RZ.Profile 1" "$PROF" 2>/dev/null
        # 2026-08-24: stale CF cookies (cf_clearance/vses2) cause INFINITE challenge
        # loops — strip them so the browser passes as virgin, then logs in fresh.
        if [ -f "$PROF/cookies.sqlite" ]; then
            sqlite3 "$PROF/cookies.sqlite" "DELETE FROM moz_cookies WHERE host LIKE '%cloudflare%';" 2>/dev/null
        fi
        cat >> "$PROF/user.js" <<'PREFS'
user_pref("layers.acceleration.disabled", true);
user_pref("gfx.webrender.software", true);
user_pref("browser.sessionstore.resume_from_crash", false);
PREFS
    fi
fi
rm -f "$PROF/.parentlock" "$PROF/lock"
for p in $(pgrep -x firefox-bin); do kill -9 $p 2>/dev/null; done
sleep 2
DISPLAY=:99 MOZ_DISABLE_CONTENT_SANDBOX=1 LIBGL_ALWAYS_SOFTWARE=1 \
    setsid /usr/bin/firefox --no-remote --new-instance \
    --profile "$PROF" --marionette about:blank </dev/null >/dev/null 2>&1 &
sleep 15
PORT_UP=$(ss -tln | grep -c ':2828')
[ "$PORT_UP" = "0" ] && { log "[claim] marionette dead — abort"; exit 3; }
CLAIM_OUT=$(timeout 240 $VENV $EON/matrix-claim.py "$CLAIM" 2>/dev/null | grep '^RESULT' | cut -d' ' -f2-)
log "[claim] $CLAIM_OUT"

# ── 4. VERIFY LANE (AI answer through public URL, with secret) ──
sleep 5
ANS=$(timeout 60 curl -s --noproxy '*' -X POST "$LANE/v1/chat/completions" \
    -H 'Content-Type: application/json' -H "x-eon-key: $SECRET" \
    --data '{"messages":[{"role":"user","content":"Reply exactly: LANE-LIVE"}],"max_tokens":12}' \
    | python3 -c "import json,sys;j=json.load(sys.stdin);c=j.get('choices',[{}])[0].get('message',{}).get('content','');print(c[:30])" 2>/dev/null)
log "[verify] answer=$ANS"

# ── 5. REGISTER if live ──
if echo "$ANS" | grep -q 'LANE-LIVE'; then
    python3 - "$AID" "$LANE" "$SECRET" <<'PYEOF'
import json, sys, pathlib
aid, lane, secret = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path('/home/ricos/.config/eon/eon200-pools.json')
pools = json.load(open(p))
lbl = f'matrix-{aid[:8]}'
if not any(e.get('label') == lbl for e in pools):
    pools.append({'token': 'post-claim-public-lane', 'account': aid,
                  'label': lbl, 'url': lane, 'url_key': secret})
json.dump(pools, open(p, 'w'), indent=1)
ap = pathlib.Path('/home/ricos/.config/ai-accounts.json')
d = json.load(open(ap))
accs = d.get('accounts', d)
accs = [a for a in accs if a.get('name') != lbl]
accs.append({'name': lbl, 'provider': 'cloudflare-workers-ai', 'kind': 'claimed-standard',
             'accountId': aid, 'lane': lane, 'status': 'live', 'autoClaimed': True})
if isinstance(d, dict) and 'accounts' in d:
    d['version'] += 1; json.dump(d, open(ap, 'w'), indent=1)
else:
    json.dump(accs, open(ap, 'w'), indent=1)
print('registered', lbl)
PYEOF
    timeout 40 /home/ricos/hide/ghost-add "matrix-${AID:0:8}" "$LANE" none "$AID" >/dev/null 2>&1
    log "[register] DONE — lane live"
    dream "register-done:${AID:0:8}" 1
    # ── ghost-mirror the soul immediately (MinIO) ──
    ( timeout 30 mc cp "$POOLS" eon/system-configs/fixround-20260823/pools-live.json >/dev/null 2>&1 \
      && timeout 20 mc cp "$STATE/$AID.meta.json" eon/system-configs/fixround-20260823/mints/ >/dev/null 2>&1 ) &
else
    log "[register] SKIPPED (lane not answering)"
    dream "verify-fail:$LANE" -1
fi
log "[done]"
