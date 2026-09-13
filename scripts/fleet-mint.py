#!/usr/bin/env python3
"""
EON FLEET MINT+DEPLOY — runs on GitHub Actions (24/7 cloud).
Mints fresh Cloudflare temp accounts, deploys ghost workers, re-enables
subdomains, and registers them. No browser needed (claim is separate).
This keeps the fleet alive even when the local device is off.
"""
import json, os, subprocess, sys, time, urllib.request, urllib.error, pathlib

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

def http(method, url, data=None, headers=None, timeout=30, raw=None, ct=None):
    h = dict(UA)
    if headers: h.update(headers)
    body = None
    if raw is not None:
        body = raw
        if ct: h['Content-Type'] = ct
    elif data is not None:
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

def mint():
    """Mint a fresh CF temp account via cf-temp-account (Go tool)."""
    try:
        r = subprocess.run(['cf-temp-account', '--accept-terms'],
                           capture_output=True, text=True, timeout=60,
                           env={k: v for k, v in os.environ.items() if k not in ('https_proxy','http_proxy','ALL_PROXY')})
        out = r.stdout.strip()
        if not out:
            out = r.stderr.strip()
        data = json.loads(out)
        return data
    except Exception as e:
        print(f"[mint] error: {e}")
        return None

def deploy_worker(aid, tok, name='eonbrain'):
    """Deploy the ghost worker to a fresh account + enable subdomain."""
    # get subdomain
    code, body = http('GET', f'https://api.cloudflare.com/client/v4/accounts/{aid}/workers/subdomain',
                      headers={'Authorization': f'Bearer {tok}'})
    try:
        sd = json.loads(body).get('result', {}).get('subdomain', '')
    except Exception:
        sd = ''
    if not sd:
        print(f"[deploy] no subdomain for {aid[:8]}")
        return None
    # read worker source
    worker_js = pathlib.Path('scripts/worker.js').read_text()
    meta = json.dumps({
        "main_module": "worker.js",
        "compatibility_date": "2025-01-01",
        "bindings": [{"type": "ai", "name": "AI"}]
    })
    bd = "----SV" + str(int(time.time()))
    raw = (f'--{bd}\r\nContent-Disposition: form-data; name="metadata"\r\n'
           f'Content-Type: application/json\r\n\r\n{meta}'
           f'\r\n--{bd}\r\nContent-Disposition: form-data; name="worker.js"; '
           f'filename="worker.js"\r\nContent-Type: application/javascript+module\r\n\r\n'
           f'{worker_js}\r\n--{bd}--\r\n')
    code, body = http('PUT', f'https://api.cloudflare.com/client/v4/accounts/{aid}/workers/scripts/{name}',
                      headers={'Authorization': f'Bearer {tok}'}, raw=raw.encode(),
                      ct='multipart/form-data; boundary=' + bd)
    if code not in (200, 201):
        print(f"[deploy] PUT failed {code}: {body[:100]}")
        return None
    # enable subdomain (critical — PUT resets it)
    code, body = http('POST', f'https://api.cloudflare.com/client/v4/accounts/{aid}/workers/scripts/{name}/subdomain',
                      headers={'Authorization': f'Bearer {tok}'}, data={'enabled': True})
    lane = f'https://{name}.{sd}.workers.dev'
    print(f"[deploy] OK {lane}")
    return lane

def main():
    target = int(os.environ.get('EON_TARGET', '1'))
    # Birth pacing (no-CPU survival algorithm): mint only up to MIN_LIVE live
    # lanes, using last run's health flags. When Ubuntu-on claiming keeps live
    # high, Actions stands down (saves minutes, stops registry bloat).
    min_live = int(os.environ.get('EON_MIN_LIVE', '6'))
    reg = load_reg()
    rounds = reg.get('rounds', [])
    live_now = sum(1 for r in rounds if r.get('status') == 'live')
    need = max(0, min(min_live - live_now, target))
    print(f"[fleet] {len(rounds)} rounds, live={live_now}, minting {need} new (target {target}, min_live {min_live})")
    if need == 0:
        print("[fleet] pacing: live count sufficient, no mint this run")
        return

    for i in range(need):
        m = mint()
        if not m or not m.get('account', {}).get('id'):
            print(f"[mint] #{i} failed")
            continue
        aid = m['account']['id']
        tok = m['account'].get('apiToken', '')
        claim = m.get('claim', {}).get('url', '')
        print(f"[mint] #{i} aid={aid[:8]}")
        lane = deploy_worker(aid, tok)
        if lane:
            rounds.append({
                'name': f'ghost-{aid[:8]}',
                'label': f'ghost-{aid[:8]}',
                'url': lane,
                'aid': aid,
                'claim_status': 'unclaimed',
                'minted': now(),
                'status': 'live',
            })
            # persist token for later claim
            pathlib.Path('state').mkdir(exist_ok=True)
            pathlib.Path(f'state/{aid}.mint.json').write_text(json.dumps(m))
            print(f"[mint] registered {lane}")
        time.sleep(2)

    reg['rounds'] = rounds
    reg['updated'] = now()
    save_reg(reg)
    print(f"[fleet] done, {len(rounds)} total")

if __name__ == '__main__':
    main()