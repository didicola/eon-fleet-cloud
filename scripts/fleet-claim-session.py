#!/usr/bin/env python3
"""
EON FLEET CLAIM — cloud session lane (GitHub Actions, no local box).

This is the cloud port of the ubuntu claim step:
  eon-matrix-renew.sh -> Firefox "mother" profile -> matrix-claim.py -> Claim

The ubuntu lane rotates logged-in Firefox "mother" profiles on a residential /
WARP egress. GitHub's bare headless Chromium is stopped by Cloudflare's
"performing security verification" wall — that is why the fleet registry showed
0 persistent. Here we reuse the mother sessions' Cloudflare cookies (secret
EON_CF_SESSION, produced by scripts/export-cf-session.py) in Playwright Firefox,
after joining the same WARP egress class the box uses. Each fresh mint
(state/*.mint.json) is opened at its claim URL and claimed; the matching
registry round is marked claim_status=claimed so fleet-cloud.py counts it.

Secret: EON_CF_SESSION = {"sessions":[{"source":...,"cookies":[...]}, ...]}
        (also accepts {"cookies":[...]} or a flat cookie list)
Never prints cookie values, claim tokens, or account emails.
"""
import glob
import json
import os
import re
import sys
import time

STATE_GLOB = 'state/*.mint.json'
REG = 'scripts/cloud_rounds.json'
MAX_AGE_MIN = int(os.environ.get('CLAIM_MAX_AGE_MIN', '50'))
CLICK_ROUNDS = 3

TOKEN_RE = re.compile(r'claimToken=[A-Za-z0-9_\-]+')
URL_RE = re.compile(r'https?://[^\s"\']+')


def redact(s, n=120):
    s = TOKEN_RE.sub('claimToken=***', str(s))
    s = URL_RE.sub('[url]', s)
    return s[:n]


def fresh():
    """Fresh claim URLs from the same-run mint state files."""
    urls = []
    now = time.time()
    for path in sorted(glob.glob(STATE_GLOB)):
        try:
            if (now - os.path.getmtime(path)) > MAX_AGE_MIN * 60:
                continue
            d = json.load(open(path))
            u = ((d.get('claim') or {}).get('url') or '').strip()
            aid = ((d.get('account') or {}).get('id') or '')
            if u.startswith('http'):
                urls.append((u, aid))
        except Exception as e:
            print(f"[claim] skip {path}: {redact(e)}")
    return urls


def normalize(cookies):
    """Firefox moz_cookies rows -> Playwright cookie dicts. Values stay in memory."""
    out = []
    for c in cookies if isinstance(cookies, list) else []:
        try:
            domain = (c.get('domain') or c.get('host') or '').strip()
            name = c.get('name')
            value = c.get('value')
            if not domain or not name or value is None:
                continue
            same = c.get('sameSite')
            if isinstance(same, int):
                same = {0: 'None', 1: 'Lax', 2: 'Strict'}.get(same, 'Lax')
            if same not in ('None', 'Lax', 'Strict'):
                same = 'Lax'
            secure = bool(c.get('secure', c.get('isSecure', True)))
            if same == 'None' and not secure:
                same = 'Lax'
            exp = c.get('expires', c.get('expiry', -1))
            try:
                exp = float(exp)
            except Exception:
                exp = -1
            if exp <= 0:
                exp = -1
            out.append({
                'name': str(name),
                'value': str(value),
                'domain': domain,
                'path': c.get('path') or '/',
                'expires': exp,
                'httpOnly': bool(c.get('httpOnly', c.get('isHttpOnly', False))),
                'secure': secure,
                'sameSite': same,
            })
        except Exception:
            continue
    return out


def load_sessions():
    """Session pool from EON_CF_SESSION (one or many mother profiles)."""
    raw = os.environ.get('EON_CF_SESSION', '').strip()
    if not raw:
        return []
    try:
        d = json.loads(raw)
    except Exception as e:
        print(f"[claim] session JSON parse error: {redact(e)}")
        return []

    sessions = []
    if isinstance(d, dict):
        if isinstance(d.get('sessions'), list):
            for s in d['sessions']:
                if isinstance(s, dict) and isinstance(s.get('cookies'), list):
                    sessions.append(normalize(s['cookies']))
                elif isinstance(s, list):
                    sessions.append(normalize(s))
        elif isinstance(d.get('cookies'), list):
            sessions.append(normalize(d['cookies']))
    elif isinstance(d, list):
        if d and isinstance(d[0], list):
            sessions = [normalize(x) for x in d]
        elif d and isinstance(d[0], dict) and isinstance(d[0].get('cookies'), list):
            sessions = [normalize(x['cookies']) for x in d if isinstance(x, dict)]
        else:
            sessions = [normalize(d)]

    # drop empties and identical cookie sets (no values are printed)
    uniq = []
    seen = set()
    for cs in sessions:
        if not cs:
            continue
        key = tuple(sorted((c['domain'], c['name'], c['value'][:8]) for c in cs))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(cs)
    return uniq


def mark_claimed(aid, verdict):
    """Record the claim in the registry so fleet-cloud.py counts it persistent."""
    if not aid or verdict not in ('CLAIMED', 'ALREADY'):
        return
    try:
        reg = json.load(open(REG))
        changed = False
        for r in reg.get('rounds', []):
            if r.get('aid') == aid:
                r['claim_status'] = 'claimed'
                r['claimed_at'] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                r['claim_verdict'] = verdict
                changed = True
        if changed:
            json.dump(reg, open(REG, 'w'), indent=2)
    except Exception as e:
        print(f"[claim] registry update failed: {redact(e)}")


def find_claim_button(page):
    """The dashboard's claim control: button role whose name contains 'claim'."""
    try:
        btns = page.get_by_role('button', name=re.compile(r'claim', re.I))
        n = btns.count()
        for i in range(min(n, 8)):
            b = btns.nth(i)
            try:
                if b.is_visible():
                    return b
            except Exception:
                pass
    except Exception:
        pass
    # fallback: button/link text containing 'claim' (e.g. 'Claim account')
    try:
        for b in page.locator('button, a, [role="button"]').all():
            try:
                t = (b.inner_text(timeout=1500) or '').strip().lower()
                if t and 'claim' in t and b.is_visible():
                    return b
            except Exception:
                pass
    except Exception:
        pass
    return None


CLAIMED_MARKERS = (
    'has been claimed', 'already claimed', 'account claimed', 'claim successful',
    'successfully claimed', 'you now own', 'account is yours',
)
CHALLENGE_MARKERS = (
    'performing security verification', 'just a moment', 'verify you are human',
    'checking your browser', 'attention required', 'enable javascript and cookies',
)


def claim_one(context, url):
    """Open one claim URL and click the claim control (two-step modal safe)."""
    page = context.new_page()
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=45000)
        page.wait_for_timeout(5000)
        try:
            low = page.locator('body').inner_text(timeout=10000).lower()
        except Exception:
            low = ''
        if any(x in low for x in CHALLENGE_MARKERS):
            return 'CHALLENGE', 'cloudflare security check'
        if any(x in low for x in CLAIMED_MARKERS):
            return 'ALREADY', 'page already says claimed'

        clicked = 0
        for _ in range(CLICK_ROUNDS):
            btn = find_claim_button(page)
            if not btn:
                break
            try:
                btn.click(timeout=10000)
                clicked += 1
            except Exception:
                break
            page.wait_for_timeout(6000)
            try:
                low = page.locator('body').inner_text(timeout=8000).lower()
            except Exception:
                low = ''
            if any(x in low for x in CHALLENGE_MARKERS):
                return 'CHALLENGE', 'challenge appeared after click'
            if any(x in low for x in CLAIMED_MARKERS):
                return 'CLAIMED', 'confirmed after click'

        if clicked == 0:
            return 'NO-BUTTON', 'no claim control visible'
        if any(x in low for x in ('claimed', 'success')):
            return 'CLAIMED', 'page shows claimed/success'
        return 'UNKNOWN', 'clicked; no confirmation text'
    finally:
        try:
            page.close()
        except Exception:
            pass


def main():
    sessions = load_sessions()
    if not sessions:
        print('[claim] EON_CF_SESSION not set (or no cookies) — session claim skipped')
        return 0

    urls = fresh()
    print(f'[claim] {len(urls)} fresh claim URL(s), {len(sessions)} mother session(s)')
    if not urls:
        return 0

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('[claim] playwright not installed — skip')
        return 0

    results = []
    try:
        with sync_playwright() as p:
            browser = p.firefox.launch(headless=True)
            contexts = {}
            for idx, (u, aid) in enumerate(urls):
                si = idx % len(sessions)
                ctx = contexts.get(si)
                if ctx is None:
                    try:
                        ctx = browser.new_context()
                        ctx.add_cookies(sessions[si])
                    except Exception:
                        print(f'[claim] session {si + 1} rejected by browser (format); skipped')
                        contexts[si] = False
                        ctx = None
                    if ctx:
                        contexts[si] = ctx
                elif ctx is False:
                    continue
                if not ctx:
                    continue
                try:
                    v, d = claim_one(ctx, u)
                except Exception as e:
                    v, d = 'ERROR', redact(e)
                print(f"[claim] {aid[:8] if aid else '?'} [s{si + 1}] -> {v}: {d}")
                results.append((aid, v))
                mark_claimed(aid, v)
            for ctx in contexts.values():
                if ctx:
                    try:
                        ctx.close()
                    except Exception:
                        pass
            browser.close()
    except Exception as e:
        print(f'[claim] browser fatal: {redact(e)}')
        return 0

    ok = sum(1 for _, v in results if v in ('CLAIMED', 'ALREADY'))
    print(f'[claim] done: {ok}/{len(results)} claimed-or-already')
    return 0


if __name__ == '__main__':
    sys.exit(main())
