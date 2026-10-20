#!/usr/bin/env python3
"""
EON CF SESSION EXPORT — run on the ubuntu box (or any host with the mother profiles).

The ubuntu fleet lane claims temporary Cloudflare accounts through logged-in
Firefox "mother" profiles (~/.config/eon/mothers/*) driven over Marionette by
eon-matrix-renew.sh -> matrix-claim.py. This script extracts those sessions'
Cloudflare cookies so the exact claim can run in GitHub Actions with no box.

Output (stdout) is JSON:
  {"sessions": [
     {"source": "<profile path>",
      "cookies": [{domain,name,path,value,expires,secure,httpOnly,sameSite}, ...]},
     ...
  ]}

Put the output in the GitHub secret EON_CF_SESSION
(repo Settings -> Secrets and variables -> Actions -> New repository secret).
Treat it as a credential: never commit it, never paste it in chat/public logs.

Usage on the box:
  curl -fsSL https://raw.githubusercontent.com/didicola/eon-fleet-cloud/main/scripts/export-cf-session.py \
    | python3 > ~/cf_session.json
  chmod 600 ~/cf_session.json

The DB is copied with its -wal/-shm siblings first, so a running Firefox cannot
block or truncate the export. The mother profiles are read before the default
Firefox profiles; duplicates are removed per profile.
"""
import glob
import json
import os
import shutil
import sqlite3
import sys
import tempfile


def profile_dbs():
    pats = [
        os.path.expanduser('~/.config/eon/mothers/*/cookies.sqlite'),
        os.path.expanduser('~/.mozilla/firefox/*/cookies.sqlite'),
    ]
    seen = set()
    for pat in pats:
        for path in sorted(glob.glob(pat)):
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            yield path


def read_db(path):
    """Copy the DB + WAL to a temp dir and read the cloudflare cookies."""
    tmp = tempfile.mkdtemp(prefix='cf-cookies-')
    dst = os.path.join(tmp, 'cookies.sqlite')
    try:
        for suffix in ('', '-wal', '-shm'):
            src = path + suffix
            if os.path.exists(src):
                shutil.copy2(src, dst + suffix)
        db = sqlite3.connect(dst)
        rows = db.execute(
            "select host,name,path,value,expiry,isSecure,isHttpOnly,sameSite "
            "from moz_cookies where host like '%cloudflare.com' order by expiry desc"
        ).fetchall()
        db.close()
        return rows
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    sessions = []
    for path in profile_dbs():
        try:
            rows = read_db(path)
        except Exception as e:
            print(f"[export] skip {path}: {e}", file=sys.stderr)
            continue

        cookies = []
        seen = set()
        for host, name, p, value, expiry, sec, http, same in rows:
            key = (host, name, p)
            if key in seen:
                continue
            seen.add(key)
            cookies.append({
                'domain': host,
                'name': name,
                'path': p,
                'value': value,
                'expires': expiry if expiry else -1,
                'secure': bool(sec),
                'httpOnly': bool(http),
                'sameSite': same,
            })
        if cookies:
            sessions.append({'source': path, 'cookies': cookies})
        print(f"[export] {path}: {len(cookies)} cloudflare cookie(s)", file=sys.stderr)

    print(json.dumps({'sessions': sessions}))
    print(f"[export] total {len(sessions)} session(s); stdout is the secret JSON",
          file=sys.stderr)


if __name__ == '__main__':
    main()
