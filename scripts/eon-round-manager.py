#!/usr/bin/env python3
"""
EON ROUND MANAGER — self-sustaining workers.dev round autogenesis.
PI memory (cloud_rounds.json) + dreaming (dream_stdp.json) + cloud KV (/do/fleet/rounds).

Every minted account = one "round" of permanent cloud ghost.
Rounds live ~1h on the cfat_ token; claimed rounds live forever (but unupdatable).
This manager:
  1. Reads the PI round registry (memory)
  2. Health-checks every round via its /status URL
  3. Dreams the outcome (+1 live / -1 dead) into the STDP learner
  4. When live rounds < TARGET, mints a replacement (cf-temp-account) and deploys v6 ghost
  5. Pushes the registry EVERYWHERE so the fleet is discoverable even if this laptop is off:
       - every live ghost's /do/fleet/rounds (redundant directory)
       - the Fleet Board dashboard /update (permanent URL, token-gated)
       - Telegram (channel or chat) — the live URL feed
  6. Alerts Telegram immediately when a NEW ghost is minted
"""
import json, time, subprocess, pathlib, sys, urllib.request, os

REG = pathlib.Path('/home/ricos/eon-cloud-agent/cloud_rounds.json')
DREAM = pathlib.Path('/home/ricos/eon-cloud-agent/dream_stdp.json')
POOLS = pathlib.Path('/home/ricos/.config/eon/eon200-pools.json')
STATE = pathlib.Path('/home/ricos/.config/eon/renewer-state')
TARGET = int(sys.argv[1]) if len(sys.argv) > 1 else 3
CLOUD = "https://eonbrain.quasar-ankle.workers.dev"
UA = {'User-Agent': 'eon-round-manager/1.0'}

# ─── Fleet Board config (permanent dashboard + directory) ───
try:
    _fbc = json.loads(pathlib.Path('/home/ricos/eon-stack/fleet-board-config.json').read_text())
except Exception:
    _fbc = {}

# ─── Telegram broadcast (the live URL feed) ───
TG_BOT = os.environ.get('EON_TG_BOT', _fbc.get('telegram_bot', '8814216816:AAHF2da0Ck0y8phnotragkk6Y0sjlCVFC5Q'))
TG_CHAT = os.environ.get('EON_TG_CHAT', _fbc.get('telegram_chat', '6663994526'))
TG_PROXY = os.environ.get('EON_TG_PROXY', 'socks5h://127.0.0.1:1080')

# ─── Fleet Board dashboard (permanent directory + live UI) ───
DASHBOARD_URL = os.environ.get('EON_DASHBOARD_URL', _fbc.get('dashboard_url', ''))
DASHBOARD_SECRET = os.environ.get('EON_DASHBOARD_SECRET', _fbc.get('dashboard_secret', ''))
TG_CHANNEL = os.environ.get('EON_TG_CHANNEL', _fbc.get('telegram_channel', ''))

def now(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def dream(stimulus, outcome):
    try:
        d = json.loads(DREAM.read_text())
    except Exception:
        d = {}
    d.setdefault('traces', []).append({'t': int(time.time()*1000), 'stimulus': stimulus[:100], 'outcome': float(outcome)})
    DREAM.write_text(json.dumps(d))

def load_reg():
    try:
        return json.loads(REG.read_text())
    except Exception:
        return {"updated": now(), "rounds": []}

def save_reg(reg):
    reg["updated"] = now()
    REG.write_text(json.dumps(reg, indent=1))

def health(url):
    try:
        req = urllib.request.Request(url + "/status", headers=UA)
        r = urllib.request.urlopen(req, timeout=12)
        if r.status != 200:
            return "dead"
        d = json.loads(r.read().decode())
        return "live" if d.get("ok") else "dead"
    except Exception:
        return "dead"

def tg_send(text):
    """Post to Telegram: channel if configured, else the chat. Direct first, SOCKS fallback."""
    target = TG_CHANNEL if TG_CHANNEL else TG_CHAT
    data = urllib.parse.urlencode({'chat_id': target, 'text': text, 'disable_web_page_preview': 'true'}).encode()
    url = f"https://api.telegram.org/bot{TG_BOT}/sendMessage"
    for proxy in (None, TG_PROXY):
        try:
            if proxy:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': proxy}))
            else:
                opener = urllib.request.build_opener()
            req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/x-www-form-urlencoded'})
            r = opener.open(req, timeout=20)
            d = json.loads(r.read().decode())
            if d.get('ok'):
                return True
        except Exception as e:
            last_err = e
    print(f"  [tg] send failed: {last_err}")
    return False

def push_registry(rounds, primary, live_urls):
    """Push the registry to every live ghost + the dashboard + Telegram."""
    payload = json.dumps({"rounds": rounds, "primary": primary, "updated": now(), "source": "round-manager"}).encode()
    # 1. every live ghost's /do/fleet/rounds (redundant directory)
    for u in live_urls:
        try:
            req = urllib.request.Request(u + "/do/fleet/rounds", data=payload,
                                         headers={**UA, 'Content-Type': 'application/json'}, method='POST')
            urllib.request.urlopen(req, timeout=15)
            print(f"  [kv] registry pushed to {u}")
        except Exception as e:
            print(f"  [kv] push to {u} failed: {e}")
    # 2. the Fleet Board dashboard (permanent URL, token-gated)
    if DASHBOARD_URL and DASHBOARD_SECRET:
        try:
            req = urllib.request.Request(DASHBOARD_URL + "/update?token=" + DASHBOARD_SECRET, data=payload,
                                         headers={**UA, 'Content-Type': 'application/json'}, method='POST')
            urllib.request.urlopen(req, timeout=15)
            print(f"  [board] registry pushed to {DASHBOARD_URL}")
        except Exception as e:
            print(f"  [board] push failed: {e}")
    # 3. Telegram — the live URL feed (viewable from anywhere, even with laptop off)
    lines = [f"◈ FLEET REGISTRY — {now()} — live={len(live_urls)}/{len(rounds)}"]
    for r in rounds:
        mark = "🟢" if r.get("health") == "live" else "🔴"
        prim = " ⭐PRIMARY" if r.get("url") == primary else ""
        lines.append(f"{mark} {r.get('label','?')}: {r.get('url')}{prim}")
    lines.append(f"Board: {DASHBOARD_URL or 'not deployed'}")
    tg_send("\n".join(lines))

def mint_and_deploy():
    """Delegate to eon-matrix-renew.sh (full mint→deploy→claim→verify→register).
    Returns (aid, url, tok, ai_url, claim_status) or None."""
    try:
        r = subprocess.run(
            ["bash", "/home/ricos/eon-stack/eon-matrix-renew.sh"],
            capture_output=True, text=True, timeout=420)
        out = r.stdout + r.stderr
        # read the minted account from /tmp/mr-mint.json (renew writes it)
        import json as _j
        mint = _j.loads(open('/tmp/mr-mint.json').read())
        aid = mint['account']['id']
        tok = mint['account']['apiToken']
        url = mint['claim']['url']
        # claim_status from renew log
        claim_status = "claimed" if ('claimed' in out.lower() or 'registered' in out.lower()) else "unclaimed"
        # deploy AI proxy (fresh 10k neurons/day) — renew doesn't do this
        ai_url = ""
        try:
            aidep = subprocess.run(
                ["bash", "/home/ricos/eon-stack/eon-deploy-ai-proxy.sh", aid, tok],
                capture_output=True, text=True, timeout=120)
            ai_url = aidep.stdout.strip().splitlines()[-1] if aidep.stdout.strip() else ""
            if ai_url.startswith("https://"):
                print(f"  [mint] AI proxy live: {ai_url}")
        except Exception as e:
            print(f"  [mint] AI proxy deploy failed: {e}")
        dream(f"round-deploy:{aid[:8]}", 1)
        return aid, url, tok, ai_url, claim_status
    except Exception as e:
        print(f"[mint] FAILED: {e}")
        return None

def load_manifest():
    try:
        return json.loads(pathlib.Path('/home/ricos/eon-cloud-agent/cloud-zero-local-manifest.json').read_text())
    except Exception:
        return {}

def answer_pending_questions():
    """Poll the ghost dashboard for queued chat questions and answer them via the local own-cloud gateway (:8093)."""
    if not DASHBOARD_URL:
        return
    try:
        req = urllib.request.Request(DASHBOARD_URL + "/pending?token=" + DASHBOARD_SECRET, headers=UA)
        pending = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
    except Exception as e:
        print(f"  [brain] pending poll failed: {e}")
        return
    if not pending:
        return
    print(f"  [brain] {len(pending)} queued question(s) — answering via local gateway")
    for q in pending:
        question = q.get("question", "")
        answer = ""
        try:
            # local own-cloud gateway (13 models, $0, unlimited)
            g = urllib.request.Request("http://127.0.0.1:8093/v1/chat/completions",
                                       data=json.dumps({
                                           "model": "auto",
                                           "messages": [
                                               {"role": "system", "content": "You are ghost.eon, a sovereign edge entity. You remember, you act, you persist. Answer briefly and honestly."},
                                               {"role": "user", "content": question},
                                           ],
                                           "max_tokens": 300,
                                       }).encode(),
                                       headers={'Content-Type': 'application/json'}, method='POST')
            r = json.loads(urllib.request.urlopen(g, timeout=60).read().decode())
            answer = (r.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            print(f"  [brain] local gateway failed for q: {e}")
            answer = "(local brain offline — question remains queued)"
        try:
            req = urllib.request.Request(DASHBOARD_URL + "/answer?token=" + DASHBOARD_SECRET,
                                         data=json.dumps({"ts": q.get("ts"), "question": question, "answer": answer}).encode(),
                                         headers={**UA, 'Content-Type': 'application/json'}, method='POST')
            urllib.request.urlopen(req, timeout=15)
            print(f"  [brain] answered: {question[:60]} → {answer[:60]}")
        except Exception as e:
            print(f"  [brain] answer post failed: {e}")

def main():
    reg = load_reg()
    rounds = reg["rounds"]
    live = 0
    live_urls = []
    # Option B: verify cloud twins from the 0-local manifest are reachable
    manifest = load_manifest()
    twins = [s for s in manifest.get('local_services', []) if s.get('status') == 'LIVE']
    print(f"[0-local] {len(twins)} cloud twins in manifest (Option B)")
    print(f"[rounds] {now()} checking {len(rounds)} rounds (target={TARGET})")
    for rnd in rounds:
        h = health(rnd["url"])
        rnd["health"] = h
        rnd["last_check"] = now()
        if h == "live":
            live += 1
            live_urls.append(rnd["url"])
            dream(f"round-live:{rnd['label']}", 1)
        else:
            dream(f"round-dead:{rnd['label']}", -1)
        print(f"  {rnd['label']}: {h}")

    # mint replacements for dead rounds
    need = TARGET - live
    if need > 0:
        print(f"[mint] {need} replacement(s) needed")
        for i in range(need):
            res = mint_and_deploy()
            if res:
                aid, url, tok, ai_url, claim_status = res
                rounds.append({
                    "aid": aid, "url": url, "label": url.split(".")[1],
                    "deployed_at": now(), "token_expires": "~1h",
                    "claim_status": claim_status, "health": "live", "version": "v6",
                    "ai_proxy": ai_url,
                    "note": "auto-minted by round manager"
                })
                live_urls.append(url)
                live += 1
                # register in pools (post-claim placeholder)
                try:
                    pools = json.loads(POOLS.read_text())
                    lbl = f"matrix-{aid[:8]}"
                    if not any(e.get('label') == lbl for e in pools):
                        pools.append({'token': 'post-claim-public-lane', 'account': aid,
                                      'label': lbl, 'url': url, 'url_key': tok})
                        POOLS.write_text(json.dumps(pools, indent=1))
                except Exception as e:
                    print(f"  [pools] {e}")
                # persist mint json for claim
                try:
                    STATE.mkdir(exist_ok=True)
                    (STATE / f"{aid}.mint.json").write_text(json.dumps({"account": {"id": aid, "apiToken": tok}}))
                except Exception:
                    pass
                print(f"  [round] NEW: {url}")
                # immediate Telegram alert — the new URL is live NOW
                tg_send(f"🆕 NEW GHOST LIVE: {url}\nlabel={url.split('.')[1]} minted {now()}\nBoard: {DASHBOARD_URL or 'not deployed'}")
            else:
                print("  [round] mint failed — will retry next cycle")
                break
    else:
        print(f"[rounds] {live} live — target met, no mint needed")

    # push the registry EVERYWHERE (discoverability even with laptop off)
    primary = live_urls[0] if live_urls else ""
    push_registry(rounds, primary, live_urls)

    # answer queued chat questions from the ghost dashboard via the local gateway
    answer_pending_questions()

    save_reg(reg)
    print(f"[done] live={live}/{TARGET} rounds={len(rounds)}")

if __name__ == "__main__":
    main()