#!/usr/bin/env python3
"""
EON FLEET CLAIM — runs on GitHub Actions right after fleet-mint.py (same job).
Opens each fresh claimToken URL headless, clicks the Claim button, records outcome.
Best-effort: any failure leaves the lane unclaimed (status quo), job never fails.
Reads tokens from state/*.mint.json fresher than CLAIM_MAX_AGE_MIN (default 50).
Usage: python3 scripts/fleet-claim.py [--url <claim-url>]...
"""
import glob
import json
import os
import sys
import time

STATE_GLOB = 'state/*.mint.json'
MAX_AGE_MIN = int(os.environ.get('CLAIM_MAX_AGE_MIN', '50'))
PER_URL_S = int(os.environ.get('CLAIM_PER_URL_S', '75'))


def fresh_claim_urls():
    urls = [a for a in sys.argv[1:] if a.startswith('http')]
    now = time.time()
    for path in glob.glob(STATE_GLOB):
        try:
            if (now - os.path.getmtime(path)) > MAX_AGE_MIN * 60:
                continue
            d = json.load(open(path))
            u = ((d.get('claim') or {}).get('url') or '').strip()
            if u.startswith('http') and u not in urls:
                urls.append(u)
        except Exception as e:
            print(f"[claim] skip {path}: {e}")
    return urls


def claim_one(page, url):
    """Returns (verdict, detail). Never raises out (caller guards)."""
    page.goto(url, wait_until='domcontentloaded', timeout=45000)
    page.wait_for_timeout(6000)
    try:
        body = page.locator('body').inner_text(timeout=10000).lower()
    except Exception:
        body = ''
    if ('has been claimed' in body or 'already claimed' in body
            or 'success' in body[:2000]):
        return 'ALREADY', 'page shows claimed/success'
    btn = page.get_by_role('button', name='Claim')
    if btn.count() == 0:
        # fallback: any button whose text is exactly claim (case-insensitive)
        cands = page.locator('button').all()
        mine = [b for b in cands if (b.inner_text(timeout=3000) or '').strip().lower() == 'claim']
        if not mine:
            return 'NO-BUTTON', (body[:160].replace('\n', ' ') or 'no body text')
        mine[0].click(timeout=10000)
    else:
        btn.first.click(timeout=10000)
    page.wait_for_timeout(8000)
    try:
        after = page.locator('body').inner_text(timeout=10000).lower()
    except Exception:
        after = ''
    if ('has been claimed' in after or 'already claimed' in after
            or 'success' in after[:2000] or 'claimed' in after[:2000]):
        return 'CLAIMED', 'post-click state shows claimed/success'
    return 'UNKNOWN', (after[:160].replace('\n', ' ') or 'no body text')


def main():
    urls = fresh_claim_urls()
    print(f"[claim] {len(urls)} fresh claim URL(s)")
    if not urls:
        print("[claim] nothing to do (exit 0)")
        return 0
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[claim] playwright missing (exit 0, mint stays unclaimed)")
        return 0
    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=[
                '--no-sandbox', '--disable-blink-features=AutomationControlled'])
            page = browser.new_page(
                user_agent=('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                            'Chrome/131.0.0.0 Safari/537.36'))
            for u in urls:
                tag = u[-12:]
                try:
                    v, d = claim_one(page, u)
                except Exception as e:
                    v, d = 'ERROR', str(e)[:120]
                print(f"[claim] ..{tag} -> {v}: {d}")
                results.append({'url_tail': tag, 'verdict': v, 'detail': d})
            browser.close()
    except Exception as e:
        print(f"[claim] browser fatal: {str(e)[:160]} (lanes stay unclaimed)")
        return 0
    ok = sum(1 for r in results if r['verdict'] in ('CLAIMED', 'ALREADY'))
    print(f"[claim] done: {ok}/{len(results)} claimed-or-already")
    return 0


if __name__ == '__main__':
    sys.exit(main())
