#!/usr/bin/env python3
"""
EON FLEET CLAIM — cloud session lane (GitHub Actions, no local box).

Cloudflare's claim flow (developers.cloudflare.com/workers/platform/claim-deployments):
the claim URL is a bearer credential, but the person claiming must "sign in to
Cloudflare or create an account" and complete the dashboard prompts. The ubuntu
lane keeps logged-in Firefox "mother" profiles for exactly that. This script is
the cloud port:

  * mother sessions' Cloudflare cookies come from repo secret EON_CF_SESSION
    (one-time export with scripts/export-cf-session.py)
  * Playwright Firefox (headful under Xvfb in CI) uses them on a WARP egress,
    the same network class the box uses
  * every run does an authenticated dashboard keepalive visit: Cloudflare's
    dashboard session times out after 72 hours WITHOUT ACTIVITY, and this
    pipeline runs every 10 minutes, so an exported session stays warm with no
    local machine involved
  * each successful claim marks the registry round claim_status=claimed, which
    fleet-cloud.py counts as persistent

No session -> nothing to claim with; exit cleanly. Never prints cookie values,
claim tokens, or account emails. If the dashboard asks for login, prints
AUTH-REQUIRED (surfaced as a GitHub warning) and does not touch the registry.
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
CHALLENGE_WAIT_S = int(os.environ.get('CLAIM_CHALLENGE_WAIT_S', '45'))
HEADLESS = os.environ.get('CLAIM_HEADLESS', '1') != '0'

TOKEN_RE = re.compile(r'claimToken=[A-Za-z0-9_\-]+')
URL_RE = re.compile(r'https?://[^\s"\']+')

CHALLENGE_MARKERS = (
    'performing security verification', 'just a moment', 'verify you are human',
    'checking your browser', 'attention required', 'enable javascript and cookies',
)
AUTH_MARKERS = (
    'log in to cloudflare', 'sign in to cloudflare', 'forgot your password',
    'create your account', 'enter your email',
)
DASH_MARKERS = (
    'workers & pages', 'account home', 'websites', 'domain registration', 'add site',
)
CLAIMED_MARKERS = (
    'has been claimed', 'already claimed', 'account claimed', 'claim successful',
    'successfully claimed', 'you now own', 'account is yours',
)


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

    # drop empties and identical cookie sets (values never printed)
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


def body_text(page, timeout=8000):
    try:
        return page.locator('body').inner_text(timeout=timeout).lower()
    except Exception:
        return ''


def wait_out_challenge(page, seconds=CHALLENGE_WAIT_S):
    """Cloudflare's interstitial auto-resolves; give it time instead of failing at 5s."""
    end = time.time() + seconds
    low = ''
    while True:
        low = body_text(page)
        if not any(x in low for x in CHALLENGE_MARKERS):
            return low
        if time.time() >= end:
            return low
        try:
            page.wait_for_timeout(2500)
            page.wait_for_load_state('domcontentloaded', timeout=5000)
        except Exception:
            pass


def find_claim_button(page):
    """The dashboard's claim control: button whose accessible name contains 'claim'."""
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


def keepalive(context):
    """Authenticated dashboard visit: resets Cloudflare's 72h idle timeout."""
    page = context.new_page()
    try:
        page.goto('https://dash.cloudflare.com/', wait_until='domcontentloaded', timeout=45000)
        low = wait_out_challenge(page, 30)
        if any(x in low for x in CHALLENGE_MARKERS):
            return 'CHALLENGE'
        if any(x in low for x in AUTH_MARKERS):
            return 'LOGIN-REQUIRED'
        if any(x in low for x in DASH_MARKERS):
            return 'AUTHED'
        return 'UNKNOWN'
    except Exception as e:
        return 'ERROR:' + redact(e, 80)
    finally:
        try:
            page.close()
        except Exception:
            pass


def claim_one(context, url):
    """Open one claim URL and click the claim control (two-step modal safe)."""
    page = context.new_page()
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=45000)
        low = wait_out_challenge(page)
        if any(x in low for x in CHALLENGE_MARKERS):
            return 'CHALLENGE', 'security check did not clear in time'
        if any(x in low for x in AUTH_MARKERS) or '/login' in (page.url or ''):
            return 'AUTH-REQUIRED', 'session expired or not logged in'
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
            low = body_text(page)
            if any(x in low for x in CHALLENGE_MARKERS):
                low = wait_out_challenge(page, 20)
            if any(x in low for x in AUTH_MARKERS):
                return 'AUTH-REQUIRED', 'login requested after click'
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
        print('[claim] EON_CF_SESSION not set — no claim credential; skipping (mint stays live)')
        return 0

    urls = fresh()
    print(f'[claim] {len(urls)} fresh claim URL(s), {len(sessions)} session(s), headless={HEADLESS}')
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('[claim] playwright not installed — skip')
        return 0

    results = []
    try:
        with sync_playwright() as p:
            browser = p.firefox.launch(headless=HEADLESS)
            contexts = []
            for i, cs in enumerate(sessions):
                try:
                    ctx = browser.new_context()
                    ctx.add_cookies(cs)
                    contexts.append((i + 1, ctx))
                except Exception:
                    print(f'[claim] session {i + 1} rejected by browser (format); skipped')
            if not contexts:
                print('[claim] no usable sessions — skip')
                browser.close()
                return 0

            # Keepalive every pooled session: touches the dashboard so Cloudflare's
            # 72h idle timer never fires, and finds which sessions are still
            # logged in. Claims use only the live ones.
            usable = []
            for idx, ctx in contexts:
                ka = keepalive(ctx)
                print(f'[claim] keepalive session {idx} -> {ka}')
                if ka in ('AUTHED', 'UNKNOWN'):
                    usable.append((idx, ctx))
            if not usable:
                print('::warning::[claim] all EON_CF_SESSION sessions expired (dashboard asks for login) — export a fresh session')
                for _, ctx in contexts:
                    try:
                        ctx.close()
                    except Exception:
                        pass
                browser.close()
                return 0

            for n, (u, aid) in enumerate(urls):
                idx, ctx = usable[n % len(usable)]
                try:
                    v, d = claim_one(ctx, u)
                except Exception as e:
                    v, d = 'ERROR', redact(e)
                print(f"[claim] {aid[:8] if aid else '?'} [s{idx}] -> {v}: {d}")
                if v == 'AUTH-REQUIRED':
                    print('::warning::[claim] session expired during claim — export a fresh EON_CF_SESSION')
                results.append((aid, v))
                mark_claimed(aid, v)

            # Persist refreshed cookies for an optional secret write-back.
            # Only live sessions, so a stale one cannot clobber a good secret.
            try:
                dump = {'sessions': [
                    {'source': f'cloud-{idx}', 'cookies': ctx.cookies()}
                    for idx, ctx in usable
                ]}
                os.makedirs('state', exist_ok=True)
                with open('state/cf_session_refresh.json', 'w') as f:
                    json.dump(dump, f)
                print('[claim] refreshed session state captured (state/cf_session_refresh.json)')
            except Exception as e:
                print(f'[claim] refresh capture failed: {redact(e)}')

            for _, ctx in contexts:
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
