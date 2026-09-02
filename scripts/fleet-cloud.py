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

    # Health-check each round
    live = 0
    for r in rounds:
        url = r.get('url', '')
        if not url: continue
        try:
            code, body = http('GET', url.rstrip('/') + '/health', timeout=10)
            ok = code == 200
            if ok: live += 1
            r['status'] = 'live' if ok else f'dead({code})'
            r['last_check'] = now()
            print(f"  {r.get('name','?')[:20]:20} {url[:40]:40} -> {code}")
        except Exception as e:
            r['status'] = f'err:{str(e)[:30]}'
            print(f"  {r.get('name','?')[:20]:20} {url[:40]:40} -> err")
    reg['live'] = live
    reg['updated'] = now()
    save_reg(reg)
    print(f"[fleet] live={live}/{len(rounds)}")

    # Push to dashboard (permanent URL)
    dash = os.environ.get('EON_DASHBOARD_URL', '')
    secret = os.environ.get('EON_DASHBOARD_SECRET', '')
    if dash and secret:
        code, body = http('POST', dash.rstrip('/') + '/update',
                          data={'secret': secret, 'rounds': rounds},
                          timeout=20)
        print(f"[dash] push -> {code} {body[:80]}")

    # Telegram broadcast
    bot = os.environ.get('EON_TG_BOT', '')
    chat = os.environ.get('EON_TG_CHANNEL', '') or os.environ.get('EON_TG_CHAT', '')
    if bot and chat:
        msg = f"EON Fleet Cloud: {live}/{len(rounds)} live (GitHub Actions 24/7)"
        code, body = http('POST', f'https://api.telegram.org/bot{bot}/sendMessage',
                          data={'chat_id': chat, 'text': msg}, timeout=20)
        print(f"[tg] -> {code}")

    print("[fleet] done")

if __name__ == '__main__':
    main()