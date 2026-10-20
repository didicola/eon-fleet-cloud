#!/usr/bin/env python3
"""
EON FLEET CLOUD — GitHub Actions pipeline (24/7, runs on GitHub's cloud).
Health-checks every deployed ghost, pushes the registry to the permanent
dashboard (ghost.eon-sovereign.workers.dev) and broadcasts to Telegram.

This runs on GitHub Actions (ubuntu-latest), NOT on the local device, so the
fleet stays alive even when the laptop is off.
"""
import json, os, sys, time, urllib.request, urllib.error

REG = 'scripts/cloud_rounds.json'
UA = {'User-Agent': 'eon-fleet-cloud/1.0'}

def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def load_reg():
    try:
        with open(REG) as f:
            return json.load(f)
    except Exception:
        return {"updated": now(), "rounds": []}

def save_reg(data):
    with open(REG, 'w') as f:
        json.dump(data, f, indent=2)

def http(method, url, data=None, headers=None, timeout=20):
    h = dict(UA)
    if headers: h.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h.setdefault('Content-Type', 'application/json')
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')
    except Exception as e:
        return 0, str(e)

def main():
    reg = load_reg()
    rounds = reg.get('rounds', [])
    print(f"[fleet] {len(rounds)} rounds in registry")

    # Health-check each round (try /status, /api/status, /health, then /)
    # Longevity inference (no-CPU survival algorithm): a lane live past
    # TEMP_LIFE_MIN is almost certainly claimed (temp tokens die ~1h), so it
    # counts as persistent without any new data or browser. Survives restarts:
    # once persistent, always persistent unless observed dead twice in a row.
    import datetime
    TEMP_LIFE_MIN = int(os.environ.get('EON_TEMP_LIFE_MIN', '70'))
    CHECK_TIMEOUT = int(os.environ.get('EON_HEALTH_TIMEOUT', '4'))
    live = 0
    persist = 0
    for r in rounds:
        url = r.get('url', '')
        if not url: continue
        # Dead ephemeral lanes are final: a temporary preview account is deleted
        # after ~60 minutes, so its worker URL never comes back. Re-probing every
        # dead round (4 endpoints x 4s each) is what pushed each run toward the
        # 25-minute job budget. Persistent rounds are always re-checked; a
        # transient failure on a live lane is lost only until the next mint.
        if r.get('status') == 'dead' and not r.get('persistent'):
            r['last_check'] = now()
            continue
        live_ok = False
        reachable = False
        codes = []
        for ep in ('/status', '/api/status', '/health', '/'):
            try:
                code, body = http('GET', url.rstrip('/') + ep, timeout=CHECK_TIMEOUT)
                codes.append(code)
                if code == 200 and body.strip() and 'not found' not in body:
                    live_ok = True
                    break
                # Any answer that is not DNS-death means the worker still exists.
                # Cloudflare serves 403/429 challenges to datacenter egress; those
                # used to flip healthy lanes to dead and over-fire minting.
                if code and code != 404:
                    reachable = True
            except Exception:
                continue
        prev_live = r.get('status') == 'live'
        if live_ok:
            ok = True
            r['probe'] = 'live'
        elif reachable:
            ok = prev_live
            r['probe'] = 'unknown(' + (','.join(str(c) for c in codes) or '?') + ')'
        else:
            ok = False
            r['probe'] = 'dead(dns)'
        if not ok:
            r['dead_streak'] = int(r.get('dead_streak', 0)) + 1
            # Two consecutive dead probes before burying a lane that was live last
            # run: a single DNS/timeout blip must not trigger a mint.
            if prev_live and r['dead_streak'] < 2:
                ok = True
                r['probe'] = 'unknown(first dead probe)'
        # Clear the strike counter only on a real signal (200 or an ambiguous
        # answer). Clearing it on the forced first-strike grace would make the
        # two-strike rule unreachable.
        if live_ok or reachable:
            r['dead_streak'] = 0
        if ok:
            live += 1
        if ok and prev_live:
            r['live_streak'] = int(r.get('live_streak', 1)) + 1
        elif ok:
            r['live_streak'] = 1
        else:
            r['live_streak'] = 0
        try:
            age_min = (datetime.datetime.strptime(now(), "%Y-%m-%dT%H:%M:%SZ")
                       - datetime.datetime.strptime(r.get('minted', now()), "%Y-%m-%dT%H:%M:%SZ")).total_seconds() / 60
        except Exception:
            age_min = 0
        if r.get('claim_status') == 'claimed' or r.get('persistent'):
            r['persistent'] = True
        elif ok and age_min > TEMP_LIFE_MIN:
            r['persistent'] = True
            r['persistent_why'] = f'inferred: live at age {int(age_min)}min > temp life'
        if r.get('persistent') and not ok:
            if r['dead_streak'] >= 2:
                r['persistent'] = False
        if r.get('persistent') and ok:
            persist += 1
        r['status'] = 'live' if ok else 'dead'
        r['last_check'] = now()
        print(f"  {r.get('name','?')[:20]:20} {url[:40]:40} -> {'live' if ok else 'dead'}{' P' if r.get('persistent') else ''} [{r.get('probe','?')}]")
    reg['live'] = live
    reg['persistent'] = persist
    reg['updated'] = now()
    save_reg(reg)
    print(f"[fleet] live={live}/{len(rounds)} persistent={persist}")

    # Push to dashboard (permanent URL) — auth via ?token= query param
    dash = os.environ.get('EON_DASHBOARD_URL', '')
    secret = os.environ.get('EON_DASHBOARD_SECRET', '')
    if dash and secret:
        # ensure each round has url + label (dashboard requirement)
        clean = []
        for r in rounds:
            if not r.get('url'): continue
            r['label'] = r.get('label') or r.get('name') or f"ghost-{r.get('aid','?')[:8]}"
            clean.append(r)
        sep = '&' if '?' in dash else '?'
        code, body = http('POST', dash.rstrip('/') + '/update' + sep + 'token=' + secret,
                          data={'rounds': clean},
                          timeout=20)
        print(f"[dash] push -> {code} {body[:80]}")

    # Telegram broadcast
    bot = os.environ.get('EON_TG_BOT', '')
    chat = os.environ.get('EON_TG_CHANNEL', '') or os.environ.get('EON_TG_CHAT', '')
    if bot and chat:
        msg = f"EON Fleet Cloud: {live}/{len(rounds)} live ({persist} persistent) (GitHub Actions 24/7)"
        code, body = http('POST', f'https://api.telegram.org/bot{bot}/sendMessage',
                          data={'chat_id': chat, 'text': msg}, timeout=20)
        print(f"[tg] -> {code}")

    print("[fleet] done")

if __name__ == '__main__':
    main()