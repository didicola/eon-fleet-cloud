#!/usr/bin/env python3
"""
EON FLEET KEEPER — token-based ghost self-heal (no browser, no session).

Why this exists
---------------
The fleet used to claim freshly minted temp accounts from a logged-in browser
session (repo secret EON_CF_SESSION). That lane is dead: Cloudflare binds the
dashboard session to the browser TLS fingerprint, Turnstile rejects WARP /
datacenter / WebDriver clients, and every stored mint token came back 401.
Measured 2026-10-20: claim 0/3, keepalive LOGIN-REQUIRED.

The durable lane is ownership, not claiming: the mother user belongs to N
standard accounts, each with a `*.workers.dev` subdomain, and a scoped API
token (EON_CF_TOKEN) can enumerate them and (re)deploy the ghost worker with
no browser at all. This script is that lane.

What it does, per account
-------------------------
  1. read subdomain
  2. list scripts -> does `eonbrain` exist?
  3. probe https://eonbrain.<sub>.workers.dev/v1/models
  4. if missing or not answering -> PUT the ghost worker + enable subdomain
  5. probe again, record result

env
---
  EON_CF_TOKEN     required  cfut_... token (Workers Scripts:Edit, Account:Read)
  EON_KEEPER_NAME  optional  worker name (default eonbrain)
  EON_KEEPER_MAX   optional  cap accounts processed (default 40)
  EON_KEEPER_DRY   optional  "1" = verify only, never write
  EON_KEEPER_OUT   optional  path for the JSON report

Exit code is 0 unless the token itself is rejected (so a flaky lane never
red-walls the whole pipeline).
"""
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

API = 'https://api.cloudflare.com/client/v4'
UA = {'User-Agent': 'eon-fleet-keeper/1.0'}
HERE = pathlib.Path(__file__).resolve().parent


def http(method, url, data=None, headers=None, raw=None, ct=None, timeout=25):
    h = dict(UA)
    if headers:
        h.update(headers)
    body = raw
    if data is not None:
        body = json.dumps(data).encode()
        h.setdefault('Content-Type', 'application/json')
    if ct:
        h['Content-Type'] = ct
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')
    except Exception as e:
        return 0, str(e)


def api(token, method, path, data=None, raw=None, ct=None, timeout=25):
    code, body = http(method, API + path,
                      data=data, raw=raw, ct=ct,
                      headers={'Authorization': f'Bearer {token}'}, timeout=timeout)
    try:
        j = json.loads(body)
    except Exception:
        j = {'success': False, 'errors': [{'message': body[:200]}]}
    return code, j


def list_accounts(token, limit):
    """Every account the token's user belongs to (paginated)."""
    out, page = [], 1
    while len(out) < limit and page <= 10:
        code, j = api(token, 'GET', f'/accounts?page={page}&per_page=50')
        if not j.get('success'):
            return out, j
        batch = j.get('result') or []
        out.extend(batch)
        info = j.get('result_info') or {}
        if len(batch) < 50 or page >= int(info.get('total_pages') or 1):
            break
        page += 1
    return out[:limit], None


def worker_meta():
    return json.dumps({
        'main_module': 'worker.js',
        'compatibility_date': '2025-01-01',
        'bindings': [{'type': 'ai', 'name': 'AI'}],
    })


def deploy_worker(token, aid, name, source):
    bd = '----EON' + str(int(time.time()))
    raw = (
        f'--{bd}\r\nContent-Disposition: form-data; name="metadata"\r\n'
        f'Content-Type: application/json\r\n\r\n{worker_meta()}'
        f'\r\n--{bd}\r\nContent-Disposition: form-data; name="worker.js"; filename="worker.js"\r\n'
        f'Content-Type: application/javascript+module\r\n\r\n{source}'
        f'\r\n--{bd}--\r\n'
    )
    code, j = api(token, 'PUT', f'/accounts/{aid}/workers/scripts/{name}',
                  raw=raw.encode(), ct=f'multipart/form-data; boundary={bd}', timeout=120)
    if code not in (200, 201) or not j.get('success'):
        return False, f'PUT {code} {json.dumps(j.get("errors"))[:160]}'
    # a PUT resets the subdomain route — always re-enable
    code, j = api(token, 'POST', f'/accounts/{aid}/workers/scripts/{name}/subdomain',
                  data={'enabled': True})
    if not j.get('success'):
        return False, f'subdomain {code} {json.dumps(j.get("errors"))[:120]}'
    return True, 'deployed'


def probe(lane, timeout=10):
    """A ghost is live when /v1/models returns the model list."""
    code, body = http('GET', lane.rstrip('/') + '/v1/models', timeout=timeout)
    live = code == 200 and '"object"' in body and 'list' in body
    return live, code


def probe_settled(lane, tries=6, wait=6):
    """workers.dev routes propagate a few seconds after PUT — poll instead of
    declaring a fresh deploy dead on the first 404 (measured ~10-15s)."""
    ok, code = probe(lane)
    for _ in range(tries - 1):
        if ok:
            break
        time.sleep(wait)
        ok, code = probe(lane)
    return ok, code


def main():
    token = os.environ.get('EON_CF_TOKEN', '').strip()
    if not token:
        print('[keeper] EON_CF_TOKEN not set — skipping (nothing to do)')
        return 0

    name = os.environ.get('EON_KEEPER_NAME', 'eonbrain').strip() or 'eonbrain'
    limit = int(os.environ.get('EON_KEEPER_MAX', '40'))
    dry = os.environ.get('EON_KEEPER_DRY', '') == '1'
    out_path = os.environ.get('EON_KEEPER_OUT', '')

    source = (HERE / 'worker.js').read_text()

    accounts, err = list_accounts(token, limit)
    if err is not None:
        print(f'[keeper] token rejected while listing accounts: {json.dumps(err.get("errors"))[:200]}')
        return 2

    print(f'[keeper] {len(accounts)} accounts · worker={name} · dry={dry}')
    report = {'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'worker': name, 'accounts': []}
    live = healed = dead = 0

    for a in accounts:
        aid, aname = a.get('id'), a.get('name')
        row = {'id': aid, 'name': aname}
        code, j = api(token, 'GET', f'/accounts/{aid}/workers/subdomain')
        sub = ((j.get('result') or {}).get('subdomain') or '') if j.get('success') else ''
        row['subdomain'] = sub
        if not sub:
            row.update(state='no-subdomain')
            report['accounts'].append(row)
            dead += 1
            print(f'  - {aname}: no subdomain')
            continue

        lane = f'https://{name}.{sub}.workers.dev'
        row['lane'] = lane

        code, j = api(token, 'GET', f'/accounts/{aid}/workers/scripts')
        names = [s.get('id') for s in (j.get('result') or [])] if j.get('success') else []
        exists = name in names

        ok, pcode = probe(lane)
        if ok:
            row.update(state='live', http=pcode, deployed=False)
            live += 1
            print(f'  + {aname}: live')
            report['accounts'].append(row)
            continue

        if dry:
            row.update(state='would-heal' if not exists else 'would-redeploy', http=pcode)
            dead += 1
            print(f'  ! {aname}: down (http={pcode}, script_exists={exists}) — dry run')
            report['accounts'].append(row)
            continue

        done, detail = deploy_worker(token, aid, name, source)
        ok2, pcode2 = probe_settled(lane) if done else (False, pcode)
        if ok2:
            row.update(state='healed', http=pcode2, deployed=True, detail=detail)
            healed += 1
            print(f'  * {aname}: HEALED -> {lane}')
        else:
            row.update(state='failed', http=pcode2, deployed=done, detail=detail)
            dead += 1
            print(f'  x {aname}: failed ({detail}, http={pcode2})')
        report['accounts'].append(row)

    report.update(total=len(accounts), live=live, healed=healed, dead=dead)
    print(f'[keeper] summary: total={len(accounts)} live={live} healed={healed} down={dead}')

    reg_path = os.environ.get('EON_KEEPER_REG', '').strip()
    if reg_path:
        try:
            reg = json.loads(pathlib.Path(reg_path).read_text())
        except Exception:
            reg = {'updated': report['ts'], 'rounds': []}
        rounds = reg.setdefault('rounds', [])
        by_url = {r.get('url'): r for r in rounds}
        added = updated = 0
        for row in report['accounts']:
            if row.get('state') not in ('live', 'healed') or not row.get('lane'):
                continue
            entry = by_url.get(row['lane'])
            if entry is None:
                entry = {'aid': row['id'], 'url': row['lane'], 'label': row.get('subdomain'),
                         'deployed_at': report['ts'], 'claim_status': 'owned',
                         'version': 'keeper-v1', 'note': 'durable token lane'}
                rounds.append(entry)
                added += 1
            else:
                updated += 1
            entry.update(status='live', persistent=True, health='live',
                         last_check=report['ts'], live_streak=1)
        reg['updated'] = report['ts']
        pathlib.Path(reg_path).write_text(json.dumps(reg, indent=2))
        print(f'[keeper] registry {reg_path}: +{added} new lanes, {updated} refreshed, {len(rounds)} rounds total')

    if out_path:
        pathlib.Path(out_path).write_text(json.dumps(report, indent=2))
        print(f'[keeper] report -> {out_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
