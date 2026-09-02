// ═══════════════════════════════════════════════════════════════════
//  FLEET BOARD — live dashboard + directory beacon for the EON fleet
//  A permanent workers.dev page that ALWAYS shows every fleet URL:
//   - "/"       → live HTML dashboard (auto-refresh, pings every ghost)
//   - "/fleet"  → JSON registry (the phone book)
//   - "/go"     → 302 redirect to the current primary ghost
//   - "/status" → neutral status
//   - "/update" → POST (token-gated) — the round manager pushes the
//                 registry here every cycle, so the board stays fresh
//                 even when the laptop is off.
//  Deploy-before-claim → when the account is claimed, this URL is
//  PERMANENT. That fixed URL is how you always find the fleet.
// ═══════════════════════════════════════════════════════════════════
"use strict";

const NAME = "Fleet Board";
const VERSION = "fleet-board-1.0";

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
  });
}

function unauthorized() {
  return json({ ok: false, error: "unauthorized" }, 401);
}

function auth(request, env) {
  const secret = env.FLEET_SECRET || "fleet-board-default-secret";
  const url = new URL(request.url);
  const q = url.searchParams.get("token");
  const h = request.headers.get("X-Fleet-Token") || "";
  return q === secret || h === secret;
}

// ─── Registry (KV-backed) ───
async function getRegistry(env) {
  if (!env.FLEET_KV) return null;
  const raw = await env.FLEET_KV.get("registry");
  if (!raw) return null;
  try { return JSON.parse(raw); } catch { return null; }
}
async function setRegistry(env, reg) {
  if (!env.FLEET_KV) return null;
  await env.FLEET_KV.put("registry", JSON.stringify(reg));
  return true;
}

// ─── Ping a ghost (server-side, short timeout) ───
async function ping(url) {
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 4000);
    const r = await fetch(url + "/status", {
      headers: { "User-Agent": "fleet-board/1.0" },
      signal: ctrl.signal,
    });
    clearTimeout(t);
    if (!r.ok) return { live: false, code: r.status };
    const d = await r.json().catch(() => ({}));
    return { live: true, code: r.status, version: d?.version || d?.service || "ghost" };
  } catch (e) {
    return { live: false, code: 0, error: "timeout" };
  }
}

// ─── Live dashboard HTML ───
function dashboardHtml(reg, checks, selfUrl) {
  const updated = reg?.updated || "unknown";
  const primary = reg?.primary || "";
  const rows = (reg?.rounds || [])
    .map((r) => {
      const c = checks[r.url] || { live: false, code: 0 };
      const dot = c.live ? "#22c55e" : "#ef4444";
      const state = c.live ? "LIVE" : "dead";
      const isPrimary = r.url === primary;
      return `<tr>
        <td>${r.label || "?"}</td>
        <td><a href="${r.url}" target="_blank" rel="noopener">${r.url}</a></td>
        <td><span class="dot" style="background:${dot}"></span>${state}</td>
        <td>${c.version || r.version || "—"}</td>
        <td>${isPrimary ? '<span class="badge">PRIMARY</span>' : ""}</td>
      </tr>`;
    })
    .join("");
  const liveCount = Object.values(checks).filter((c) => c.live).length;
  const total = (reg?.rounds || []).length;
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fleet Board — live</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#09090b;--surface:#18181b;--surface2:#27272a;--surface3:#3f3f46;
    --border:#27272a;--border2:#3f3f46;
    --txt:#fafafa;--txt2:#a1a1aa;--txt3:#71717a;
    --accent:#818cf8;--accent2:#6366f1;--accent3:#4f46e5;
    --green:#22c55e;--red:#ef4444;--yellow:#eab308;
    --glass:rgba(24,24,27,.7);
    --shadow:0 25px 50px -12px rgba(0,0,0,.5);
    --radius:16px;--radius-sm:10px;--radius-xs:6px;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html{-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
  body{background:var(--bg);color:var(--txt);font-family:'Inter',system-ui,sans-serif;font-size:14px;line-height:1.6;min-height:100vh;padding:24px}
  ::-webkit-scrollbar{width:5px}
  ::-webkit-scrollbar-track{background:transparent}
  ::-webkit-scrollbar-thumb{background:var(--surface3);border-radius:9px}
  header{display:flex;align-items:center;gap:12px;padding:14px 24px;border:1px solid var(--border);border-radius:var(--radius);background:var(--glass);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);margin-bottom:20px}
  .logo-icon{width:32px;height:32px;border-radius:var(--radius-sm);background:linear-gradient(135deg,var(--accent2),var(--accent));display:flex;align-items:center;justify-content:center;font-weight:700;font-size:14px;color:#fff;letter-spacing:-0.5px}
  .logo-text{font-weight:600;font-size:16px;letter-spacing:-.3px}
  .badge{font-size:10px;font-weight:600;letter-spacing:.5px;padding:3px 8px;border-radius:20px;background:rgba(129,140,248,.15);color:var(--accent);border:1px solid rgba(129,140,248,.3)}
  .refresh{margin-left:auto;font-size:11px;color:var(--txt3);font-family:'JetBrains Mono',monospace}
  .sub{color:var(--txt3);font-size:12px;margin-bottom:16px;font-family:'JetBrains Mono',monospace}
  .stats{display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap}
  .stat{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);padding:12px 18px;min-width:130px}
  .stat b{font-size:22px;color:var(--accent);font-family:'JetBrains Mono',monospace}
  .stat span{display:block;font-size:11px;color:var(--txt3);letter-spacing:.3px}
  table{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;box-shadow:var(--shadow)}
  th{text-align:left;padding:12px 16px;background:var(--surface2);color:var(--txt2);font-size:11px;letter-spacing:1px;text-transform:uppercase;font-weight:600}
  td{padding:12px 16px;border-top:1px solid var(--border);vertical-align:middle;font-family:'JetBrains Mono',monospace;font-size:13px}
  a{color:var(--accent);text-decoration:none}
  a:hover{text-decoration:underline}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:8px;box-shadow:0 0 8px currentColor}
  .live{color:var(--green)}
  .dead{color:var(--red)}
  .badge-primary{font-size:10px;font-weight:600;letter-spacing:.5px;padding:2px 8px;border-radius:20px;background:rgba(34,197,94,.15);color:var(--green);border:1px solid rgba(34,197,94,.3)}
  .foot{margin-top:20px;color:var(--txt3);font-size:12px;display:flex;gap:20px;flex-wrap:wrap;font-family:'JetBrains Mono',monospace}
  .foot a{color:var(--txt2)}
  .empty{color:var(--txt3);text-align:center;padding:24px}
</style>
</head>
<body>
  <header>
    <div class="logo-icon">◈</div>
    <div class="logo-text">Fleet Board</div>
    <span class="badge">LIVE</span>
    <span class="refresh">auto-refresh 15s · ${selfUrl}</span>
  </header>
  <div class="sub">live directory of the autonomous fleet · registry updated ${updated}</div>
  <div class="stats">
    <div class="stat"><b>${liveCount}</b><span>live ghosts</span></div>
    <div class="stat"><b>${total}</b><span>registered</span></div>
    <div class="stat"><b>${primary ? "yes" : "—"}</b><span>primary set</span></div>
  </div>
  <table>
    <thead><tr><th>label</th><th>url</th><th>status</th><th>version</th><th>role</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="5" class="empty">no rounds registered yet — waiting for the round manager</td></tr>'}</tbody>
  </table>
  <div class="foot">
    <span><a href="/fleet">/fleet</a> — JSON registry</span>
    <span><a href="/go">/go</a> — redirect to primary</span>
    <span><a href="/status">/status</a> — board status</span>
  </div>
<script>
  setTimeout(() => location.reload(), 15000);
</script>
</body>
</html>`;
}

// ─── Main fetch handler ───
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method;

    // ── Public status ──
    if (path === "/status" && method === "GET") {
      return json({
        ok: true,
        service: NAME,
        version: VERSION,
        cloud: true,
        permanent: true,
        role: "fleet-directory",
        time: new Date().toISOString(),
      });
    }

    // ── Public JSON registry (the phone book) ──
    if (path === "/fleet" && method === "GET") {
      const reg = await getRegistry(env);
      if (!reg) return json({ ok: true, rounds: [], updated: "never", note: "no registry pushed yet" });
      return json(reg);
    }

    // ── Redirect to primary ghost ──
    if (path === "/go" && method === "GET") {
      const reg = await getRegistry(env);
      const primary = reg?.primary || "";
      if (primary) {
        return Response.redirect(primary, 302);
      }
      return json({ ok: false, error: "no primary set" }, 404);
    }

    // ── Token-gated update (round manager pushes registry here) ──
    if (path === "/update" && method === "POST") {
      if (!auth(request, env)) return unauthorized();
      const body = await request.json().catch(() => ({}));
      if (!body.rounds) return json({ ok: false, error: "rounds required" }, 400);
      const reg = {
        rounds: body.rounds,
        primary: body.primary || "",
        updated: new Date().toISOString(),
        source: body.source || "round-manager",
      };
      await setRegistry(env, reg);
      return json({ ok: true, stored: reg.rounds.length, updated: reg.updated });
    }

    // ── Live HTML dashboard ──
    if (path === "/" && method === "GET") {
      const reg = await getRegistry(env);
      const rounds = reg?.rounds || [];
      // Ping every ghost server-side (parallel, short timeout)
      const checks = {};
      await Promise.all(
        rounds.map(async (r) => {
          checks[r.url] = await ping(r.url);
        })
      );
      return new Response(dashboardHtml(reg, checks, url.origin), {
        status: 200,
        headers: { "Content-Type": "text/html; charset=utf-8" },
      });
    }

    return json({ ok: false, error: "not found" }, 404);
  },
};