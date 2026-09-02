#!/bin/bash
# ═══════════════════════════════════════════════════════
#  EON DEPLOY AI PROXY — tiny Workers AI proxy on a minted account
#  Each minted account = fresh 10,000 neurons/day for ghost.eon chat.
#  Usage: eon-deploy-ai-proxy.sh <account_id> <token>
# ═══════════════════════════════════════════════════════
set -u
AID="${1:?account_id required}"
TOK="${2:?token required}"
EON=/home/ricos/eon-stack
LOG=$EON/ai-proxy-deploy.log
SRC=/tmp/ai-proxy
log() { echo "$(date -u +%H:%M:%S) $*" | tee -a "$LOG"; }

# 1. ensure worker source exists
if [ ! -f "$SRC/worker.js" ]; then
    mkdir -p "$SRC"
    cp /tmp/ai-proxy-worker.js "$SRC/worker.js" 2>/dev/null \
      || { log "FATAL: no ai-proxy worker source"; exit 1; }
fi

# 2. write metadata with AI binding
cat > "$SRC/meta.json" <<MEOF
{
  "main_module": "worker.js",
  "compatibility_date": "2025-01-01",
  "compatibility_flags": ["nodejs_compat"],
  "bindings": [
    { "type": "ai", "name": "AI" }
  ]
}
MEOF

# 3. get subdomain
SDRSP=$(timeout 40 curl -s --noproxy '*' "https://api.cloudflare.com/client/v4/accounts/$AID/workers/subdomain" -H "Authorization: Bearer $TOK" 2>/dev/null | python3 -c "import json,sys;print(json.load(sys.stdin).get('result',{}).get('subdomain','none'))" 2>/dev/null)
log "[deploy] subdomain=$SDRSP"

# 4. PUT worker
PUT=$(timeout 90 curl -s --noproxy '*' -X PUT "https://api.cloudflare.com/client/v4/accounts/$AID/workers/scripts/ai-proxy" \
    -H "Authorization: Bearer $TOK" \
    -F 'metadata=@'"$SRC"'/meta.json;type=application/json' \
    -F 'worker.js=@'"$SRC"'/worker.js;type=application/javascript+module' 2>/dev/null)
echo "$PUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print('PUT success:', d.get('success')); sys.exit(0 if d.get('success') else 1)" 2>/dev/null \
  || { log "FATAL: PUT failed: $(echo "$PUT" | head -c 200)"; exit 4; }

# 5. enable subdomain route (PUT resets it to disabled — MUST re-enable)
timeout 40 curl -s --noproxy '*' -X POST "https://api.cloudflare.com/client/v4/accounts/$AID/workers/scripts/ai-proxy/subdomain" \
    -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
    --data '{"enabled":true}' > /dev/null 2>&1
sleep 2

LANE="https://ai-proxy.$SDRSP.workers.dev"
log "[deploy] AI PROXY LIVE: $LANE"
echo "$LANE"