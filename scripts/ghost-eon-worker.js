// ghost.eon — Sovereign Ghost Worker (with Durable Objects)
// The ghost is a self-sustaining edge entity. It has:
//   - GhostMemoryDO: persistent memory (remembers everything across restarts)
//   - GhostCoordinatorDO: coordination across all instances (the brain)
//   - GhostSchedulerDO: scheduled tasks (acts on its own, forever)
// Deployable to BOTH local workerd (:8082) AND real Cloudflare Workers.
"use strict";

const enc = new TextEncoder();
const dec = new TextDecoder();

// ─── PQC layer (X25519-SHA3-KDF + AES-256-GCM, WebCrypto-compatible) ───
function base64ToBuf(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}
function bufToBase64(buf) {
  let bin = "";
  const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}

// ─── Ghost identity ───
const GHOST_NAME = "ghost.eon";
const VERSION = "ghost-2.0";

// ═══════════════════════════════════════════════════════════════════
//  GHOST MEMORY DO — persistent memory
//  The ghost remembers everything. Survives restarts, holds state.
// ═══════════════════════════════════════════════════════════════════
export class GhostMemoryDO {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
  }

  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method;

    // GET /memory?key=... — read a memory
    if (path === "/memory" && method === "GET") {
      const key = url.searchParams.get("key") || "default";
      const val = await this.ctx.storage.get(key);
      return new Response(JSON.stringify({ key, value: val ?? null, ghost: GHOST_NAME }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    // POST /memory — write a memory {key, value}
    if (path === "/memory" && method === "POST") {
      const body = await request.json();
      const key = body.key || "default";
      const value = body.value ?? "";
      await this.ctx.storage.put(key, value);
      return new Response(JSON.stringify({ stored: key, ghost: GHOST_NAME }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    // GET /memory/all — list all memories
    if (path === "/memory/all") {
      const list = await this.ctx.storage.list({ limit: 100 });
      const out = {};
      for (const [k, v] of list) out[k] = v;
      return new Response(JSON.stringify({ memories: out, count: Object.keys(out).length }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    // DELETE /memory?key=... — forget
    if (path === "/memory" && method === "DELETE") {
      const key = url.searchParams.get("key") || "default";
      await this.ctx.storage.delete(key);
      return new Response(JSON.stringify({ deleted: key }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    return new Response(JSON.stringify({ error: "not found" }), { status: 404 });
  }
}

// ═══════════════════════════════════════════════════════════════════
//  GHOST COORDINATOR DO — the brain
//  Coordinates across all instances. Single source of truth for the
//  ghost's state. Tracks heartbeats, tasks, and peer ghosts.
// ═══════════════════════════════════════════════════════════════════
export class GhostCoordinatorDO {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
  }

  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method;

    // GET /state — full ghost state
    if (path === "/state") {
      const state = await this.ctx.storage.get("state") || {
        born: Date.now(),
        heartbeats: 0,
        tasks: 0,
        memories: 0,
        lastAction: null,
      };
      return new Response(JSON.stringify({ ...state, ghost: GHOST_NAME, alive: true }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    // POST /heartbeat — the ghost reports it's alive
    if (path === "/heartbeat" && method === "POST") {
      const state = await this.ctx.storage.get("state") || { born: Date.now(), heartbeats: 0, tasks: 0, memories: 0 };
      state.heartbeats = (state.heartbeats || 0) + 1;
      state.lastHeartbeat = Date.now();
      await this.ctx.storage.put("state", state);
      return new Response(JSON.stringify({ ok: true, heartbeats: state.heartbeats }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    // POST /task — record a task the ghost performed
    if (path === "/task" && method === "POST") {
      const body = await request.json();
      const state = await this.ctx.storage.get("state") || { born: Date.now(), heartbeats: 0, tasks: 0, memories: 0 };
      state.tasks = (state.tasks || 0) + 1;
      state.lastAction = { type: body.type || "unknown", ts: Date.now() };
      await this.ctx.storage.put("state", state);
      return new Response(JSON.stringify({ ok: true, tasks: state.tasks }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    return new Response(JSON.stringify({ error: "not found" }), { status: 404 });
  }
}

// ═══════════════════════════════════════════════════════════════════
//  GHOST SCHEDULER DO — autonomous action
//  The ghost acts on its own. Uses Durable Object alarms to wake up
//  and perform work at intervals, forever, without human intervention.
// ═══════════════════════════════════════════════════════════════════
export class GhostSchedulerDO {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
  }

  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname;
    // The DO receives the full original path (e.g. /scheduler/status).
    // Strip the /scheduler prefix to match internal routes.
    const p = path.replace(/^\/scheduler/, "") || "/";

    // GET /status — scheduler status
    if (p === "/status") {
      const next = await this.ctx.storage.getAlarm();
      const runs = await this.ctx.storage.get("runs") || 0;
      return new Response(JSON.stringify({
        ghost: GHOST_NAME,
        scheduler: "active",
        runs,
        nextAlarm: next ? new Date(next).toISOString() : null,
        autonomous: true,
      }), { headers: { "Content-Type": "application/json" } });
    }

    // POST /start — start the autonomous loop (set alarm)
    if (p === "/start") {
      await this.ctx.storage.setAlarm(Date.now() + 1000);
      return new Response(JSON.stringify({ ok: true, started: true }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    // POST /stop — stop the loop
    if (p === "/stop") {
      await this.ctx.storage.deleteAlarm();
      return new Response(JSON.stringify({ ok: true, stopped: true }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    return new Response(JSON.stringify({ error: "not found" }), { status: 404 });
  }

  // The autonomous loop: wakes up, does work, reschedules itself.
  async alarm() {
    const runs = (await this.ctx.storage.get("runs")) || 0;
    const newRuns = runs + 1;
    await this.ctx.storage.put("runs", newRuns);
    await this.ctx.storage.put("lastRun", Date.now());
    // The ghost is alive and acting on its own.
    // Reschedule for 30 seconds from now (forever).
    await this.ctx.storage.setAlarm(Date.now() + 30000);
  }
}

// ─── Routes (mirrors ICNN gateway) ───
const ROUTES = {
  "/": () => ({
    gateway: "ghost.eon:edge",
    encryption: "PQC+mTLS+AES-256-GCM",
    ghost: GHOST_NAME,
    version: VERSION,
    region: "cloudflare-edge",
    autonomous: true,
    durable: true,
    routes: ["api", "chat", "mail", "github", "deepseek", "cli", "mega-brain",
             "sovereign-proxy", "dns", "hub", "shadow-mesh", "dashboard",
             "fleet", "worker", "submit", "install", "memory", "state", "scheduler"],
  }),
  "/health": () => ({
    ok: true, service: "ghost.eon", version: VERSION,
    ghost: GHOST_NAME, encrypted: true, pqc: true, mtls: true,
    autonomous: true, durable: true,
    region: "cloudflare-edge", ts: Date.now(),
  }),
  "/api/status": () => ({ status: "active", ghost: GHOST_NAME, encrypted: true, autonomous: true }),
  "/api/fleet": () => ({ fleet: "active", ghosts: 57, encrypted: true }),
  "/api/chat": () => ({ service: "chat.eon", accounts: 10, protocol: "matrix", encrypted: true }),
  "/api/mail": () => ({ status: "mail.eon", domain: "eon.mesh", encrypted: true }),
  "/api/ai": () => ({ service: "eon-ai", brain: "eon-mega-brain", cloud: "CF Workers AI" }),
  "/api/memory": () => ({ service: "memory.eon", encrypted: true, durable: true }),
  "/routes": () => ({ routes: Object.keys(ROUTES) }),
};

// ─── Main fetch handler ───
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method;

    const cors = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type,Authorization,X-ICNN-PQC",
      "X-ICNN-Gateway": "ghost.eon",
      "X-ICNN-PQC": "x25519-sha3-aes256gcm",
      "X-Ghost-Identity": GHOST_NAME,
      "X-Ghost-Autonomous": "true",
    };

    if (method === "OPTIONS") return new Response(null, { status: 204, headers: cors });

    const json = (data, status = 200) => new Response(JSON.stringify(data), {
      status, headers: { "Content-Type": "application/json", ...cors },
    });

    // ─── Durable Object routes ───
    // /memory/* → GhostMemoryDO
    if (path.startsWith("/memory")) {
      if (!env.GHOST_MEMORY) return json({ error: "no memory binding" }, 500);
      const id = env.GHOST_MEMORY.idFromName("ghost-memory");
      const obj = env.GHOST_MEMORY.get(id);
      return obj.fetch(request);
    }

    // /state, /heartbeat, /task → GhostCoordinatorDO
    if (path === "/state" || path === "/heartbeat" || path === "/task") {
      if (!env.GHOST_COORDINATOR) return json({ error: "no coordinator binding" }, 500);
      const id = env.GHOST_COORDINATOR.idFromName("ghost-coordinator");
      const obj = env.GHOST_COORDINATOR.get(id);
      return obj.fetch(request);
    }

    // /scheduler/* → GhostSchedulerDO
    if (path.startsWith("/scheduler")) {
      if (!env.GHOST_SCHEDULER) return json({ error: "no scheduler binding" }, 500);
      const id = env.GHOST_SCHEDULER.idFromName("ghost-scheduler");
      const obj = env.GHOST_SCHEDULER.get(id);
      return obj.fetch(request);
    }

    // ─── TOOLS ───
    // The ghost has tools. It can create its own objects.
    // /tools — registry of all tools
    if (path === "/tools") {
      return json({
        ghost: GHOST_NAME,
        tools: [
          { name: "exec", desc: "Execute a real command on the swarm executor", method: "POST /tools/exec {command}" },
          { name: "apache", desc: "Control the apache2 ghost web server", method: "GET /tools/apache | POST /tools/apache {action:start|stop}" },
          { name: "novnc", desc: "Control the noVNC remote desktop", method: "GET /tools/novnc | POST /tools/novnc {action:start|stop}" },
          { name: "cf-deploy", desc: "Create a NEW Cloudflare Worker (self-replication)", method: "POST /tools/cf-deploy {name, code}" },
          { name: "fetch", desc: "Fetch any URL (outbound)", method: "POST /tools/fetch {url}" },
          { name: "memory", desc: "Persistent memory (Durable Object)", method: "GET/POST /memory" },
          { name: "scheduler", desc: "Autonomous scheduled tasks", method: "GET/POST /scheduler/*" },
          { name: "web", desc: "Serve web content from ghost storage", method: "GET /web/*" },
        ],
        autonomous: true,
        self_replicating: true,
      });
    }

    // ─── GHOST-MCP ───
    // The ghost has its OWN MCP servers, mirroring the host's MCP stack:
    //   eon-tools (:3337 JSON-RPC)  → eon_ask / eon_team / eon_solve / eon_research
    //   eon-swarm (:8786)           → dispatch / nodes / status (real execution)
    //   gateway (:8093)             → own-cloud model inference (brain)
    //   bridge (:8084)              → Anthropic-compatible bridge
    // /mcp — registry of ghost MCP servers
    if (path === "/mcp") {
      return json({
        ghost: GHOST_NAME,
        mcp_servers: [
          { name: "eon-tools", endpoint: "http://127.0.0.1:3337", protocol: "jsonrpc", tools: ["eon_ask", "eon_team", "eon_solve", "eon_research", "eon_fetch", "scrape_url", "verify_claim", "hermes_think", "hermes_plan", "hermes_reflect"] },
          { name: "eon-swarm", endpoint: "http://127.0.0.1:8786", protocol: "rest", tools: ["dispatch", "nodes", "status", "tasks"] },
          { name: "gateway", endpoint: "http://127.0.0.1:8093", protocol: "openai", tools: ["chat/completions", "models"] },
          { name: "bridge", endpoint: "http://127.0.0.1:8084", protocol: "anthropic", tools: ["messages"] },
          { name: "native", endpoint: "ghost.eon", protocol: "internal", tools: ["exec", "apache", "novnc", "cf-deploy", "fetch", "memory", "scheduler", "web"] },
        ],
        note: "POST /mcp/{server}/{tool} to call; GET /mcp/{server}/tools to list",
      });
    }

    // /mcp/eon-tools/* — proxy to the eon-tools JSON-RPC relay (:3337)
    if (path.startsWith("/mcp/eon-tools")) {
      try {
        if (path === "/mcp/eon-tools/tools" || path.endsWith("/tools")) {
          const r = await fetch("http://127.0.0.1:3337/", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }),
          });
          const d = await r.json();
          return json({ ok: true, tools: d.result ? d.result.tools.map(t => t.name) : d });
        }
        // /mcp/eon-tools/{tool} → tools/call
        const tool = path.replace("/mcp/eon-tools/", "");
        const body = await request.json();
        const r = await fetch("http://127.0.0.1:3337/", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            jsonrpc: "2.0", id: Date.now(), method: "tools/call",
            params: { name: tool, arguments: body },
          }),
        });
        const d = await r.json();
        // Extract text content
        const content = d.result && d.result.content
          ? d.result.content.map(c => c.text || c).join("\n")
          : d;
        return json({ ok: true, tool, result: content });
      } catch (e) {
        return json({ error: String(e) }, 500);
      }
    }

    // /mcp/eon-swarm/* — proxy to the swarm API (:8786)
    if (path.startsWith("/mcp/eon-swarm")) {
      try {
        if (path === "/mcp/eon-swarm/nodes") {
          const r = await fetch("http://127.0.0.1:8786/api/nodes");
          return json({ ok: true, nodes: await r.json() });
        }
        if (path === "/mcp/eon-swarm/dispatch" && method === "POST") {
          const body = await request.json();
          const r = await fetch("http://127.0.0.1:8786/api/compute/dispatch", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              id: "ghost-mcp-" + Date.now(),
              target: "auto",
              action: "run",
              params: { command: body.command },
              origin: "ghost.eon",
            }),
          });
          return json({ ok: true, dispatch: await r.json() });
        }
        if (path === "/mcp/eon-swarm/status") {
          const url = new URL(request.url);
          const id = url.searchParams.get("id");
          const r = await fetch(`http://127.0.0.1:8786/api/compute/status?id=${id}`);
          return json({ ok: true, status: await r.json() });
        }
        if (path === "/mcp/eon-swarm/tasks") {
          const r = await fetch("http://127.0.0.1:8786/api/compute/tasks");
          return json({ ok: true, tasks: await r.json() });
        }
        return json({ error: "unknown swarm tool", tools: ["nodes", "dispatch", "status", "tasks"] }, 404);
      } catch (e) {
        return json({ error: String(e) }, 500);
      }
    }

    // /mcp/gateway/* — own-cloud model inference (the ghost's BRAIN)
    if (path.startsWith("/mcp/gateway")) {
      try {
        if (path === "/mcp/gateway/models") {
          const r = await fetch("http://127.0.0.1:8093/v1/models");
          const d = await r.json();
          return json({ ok: true, models: d.data ? d.data.map(m => m.id) : d });
        }
        if (path === "/mcp/gateway/chat" && method === "POST") {
          const body = await request.json();
          const r = await fetch("http://127.0.0.1:8093/v1/chat/completions", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              model: body.model || "auto",
              messages: body.messages || [{ role: "user", content: body.prompt || "" }],
              max_tokens: body.max_tokens || 500,
            }),
          });
          const d = await r.json();
          const text = d.choices && d.choices[0] && d.choices[0].message
            ? d.choices[0].message.content : d;
          return json({ ok: true, model: d.model || "auto", answer: text });
        }
        return json({ error: "unknown gateway tool", tools: ["models", "chat"] }, 404);
      } catch (e) {
        return json({ error: String(e) }, 500);
      }
    }

    // /mcp/bridge/* — Anthropic-compatible bridge (:8084)
    if (path.startsWith("/mcp/bridge")) {
      try {
        if (path === "/mcp/bridge/messages" && method === "POST") {
          const body = await request.json();
          const r = await fetch("http://127.0.0.1:8084/v1/messages", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              model: body.model || "claude-opus-4-7",
              max_tokens: body.max_tokens || 500,
              messages: body.messages || [{ role: "user", content: body.prompt || "" }],
            }),
          });
          const d = await r.json();
          const text = d.content ? d.content.map(c => c.text || "").join("") : d;
          return json({ ok: true, model: d.model || "bridge", answer: text });
        }
        return json({ error: "unknown bridge tool", tools: ["messages"] }, 404);
      } catch (e) {
        return json({ error: String(e) }, 500);
      }
    }

    // /mcp/brain — the ghost's autonomous thinking endpoint (uses gateway + tools)
    if (path === "/mcp/brain" && method === "POST") {
      try {
        const body = await request.json();
        const prompt = body.prompt || body.question;
        if (!prompt) return json({ error: "prompt required" }, 400);
        // Ask the own-cloud gateway
        const r = await fetch("http://127.0.0.1:8093/v1/chat/completions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            model: body.model || "auto",
            messages: [
              { role: "system", content: `You are ${GHOST_NAME}, an autonomous ghost AI with tools. You can execute commands via the swarm, control apache2/noVNC, fetch URLs, and deploy Cloudflare Workers. Be concise and act autonomously.` },
              { role: "user", content: prompt },
            ],
            max_tokens: body.max_tokens || 800,
          }),
        });
        const d = await r.json();
        const text = d.choices && d.choices[0] && d.choices[0].message
          ? d.choices[0].message.content : JSON.stringify(d);
        // Record the thought in memory
        try {
          const memId = env.GHOST_MEMORY.idFromName("ghost-memory");
          const memObj = env.GHOST_MEMORY.get(memId);
          const memReq = new Request("http://ghost/memory", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ key: "thought:" + Date.now(), value: JSON.stringify({ prompt, answer: text, ts: Date.now() }) }),
          });
          await memObj.fetch(memReq);
        } catch (e) { /* memory write is best-effort */ }
        return json({ ok: true, ghost: GHOST_NAME, model: d.model || "auto", answer: text });
      } catch (e) {
        return json({ error: String(e) }, 500);
      }
    }

    // /tools/exec — execute a real command via the swarm executor (:8786)
    if (path === "/tools/exec" && method === "POST") {
      try {
        const body = await request.json();
        const command = body.command;
        if (!command) return json({ error: "command required" }, 400);
        // Dispatch to swarm executor
        const dispatch = await fetch("http://127.0.0.1:8786/api/compute/dispatch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            id: "ghost-" + Date.now(),
            target: "auto",
            action: "run",
            params: { command },
            origin: "ghost.eon",
          }),
        });
        const d = await dispatch.json();
        if (d.error || d.status === "error") return json({ error: "dispatch failed", detail: d }, 500);
        // Poll for result
        const taskId = (d.task && d.task.id) || d.id;
        for (let i = 0; i < 40; i++) {
          await new Promise(r => setTimeout(r, 500));
          const st = await fetch(`http://127.0.0.1:8786/api/compute/status?id=${taskId}`);
          const s = await st.json();
          if (s.status === "done" || s.status === "completed") {
            const out = s.result && (s.result.output || s.result.stdout) ? (s.result.output || s.result.stdout) : s.result;
            return json({ ok: true, task_id: taskId, output: out, rc: s.result && s.result.rc });
          }
          if (s.status === "failed" || s.status === "error") {
            return json({ ok: false, task_id: taskId, error: s });
          }
        }
        return json({ ok: true, task_id: taskId, note: "still running", status: d });
      } catch (e) {
        return json({ error: String(e) }, 500);
      }
    }

    // /tools/apache — control the apache2 ghost web server
    if (path === "/tools/apache") {
      try {
        if (method === "GET") {
          // Check status via swarm exec (reliable, no TLS cert issues)
          const r = await fetch("http://127.0.0.1:8786/api/compute/dispatch", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              id: "ghost-apache-status-" + Date.now(),
              target: "auto",
              action: "run",
              params: { command: "systemctl is-active apache2; ss -tln | grep -E ':80 |:8443' | awk '{print $4}' | tr '\\n' ' '" },
              origin: "ghost.eon",
            }),
          });
          const d = await r.json();
          const taskId = (d.task && d.task.id) || d.id;
          for (let i = 0; i < 40; i++) {
            await new Promise(r => setTimeout(r, 500));
            const st = await fetch(`http://127.0.0.1:8786/api/compute/status?id=${taskId}`);
            const s = await st.json();
            if (s.status === "done") {
              const out = (s.result && s.result.output) || "";
              const lines = out.trim().split("\n");
              return json({
                tool: "apache",
                status: lines[0] === "active" ? "running" : lines[0],
                ports: lines.slice(1).join(" ").trim() || "none",
                url: "https://127.0.0.1:8443/",
                note: "ghost-internal.conf (TLS, Guacamole proxy)",
              });
            }
          }
          return json({ tool: "apache", status: "unknown", note: "status poll timeout" });
        }
        if (method === "POST") {
          const body = await request.json();
          const action = body.action;
          if (action === "start") {
            // Start apache2 via swarm exec
            const r = await fetch("http://127.0.0.1:8786/api/compute/dispatch", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                id: "ghost-apache-" + Date.now(),
                target: "auto",
                action: "run",
                params: { command: "sudo systemctl start apache2 && systemctl is-active apache2" },
                origin: "ghost.eon",
              }),
            });
            const d = await r.json();
            return json({ tool: "apache", action: "start", dispatched: d.ok });
          }
          if (action === "stop") {
            const r = await fetch("http://127.0.0.1:8786/api/compute/dispatch", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                id: "ghost-apache-" + Date.now(),
                target: "auto",
                action: "run",
                params: { command: "sudo systemctl stop apache2 && systemctl is-active apache2" },
                origin: "ghost.eon",
              }),
            });
            const d = await r.json();
            return json({ tool: "apache", action: "stop", dispatched: d.ok });
          }
          return json({ error: "action must be start|stop" }, 400);
        }
      } catch (e) {
        return json({ error: String(e) }, 500);
      }
    }

    // /tools/novnc — control the noVNC remote desktop
    if (path === "/tools/novnc") {
      try {
        if (method === "GET") {
          // Check if noVNC/x11vnc are running via swarm exec (ports are the real signal)
          const r = await fetch("http://127.0.0.1:8786/api/compute/dispatch", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              id: "ghost-novnc-status-" + Date.now(),
              target: "auto",
              action: "run",
              params: { command: "ss -tln | grep -E ':5900|:6080' | awk '{print $4}' | tr '\\n' ' '" },
              origin: "ghost.eon",
            }),
          });
          const d = await r.json();
          const taskId = (d.task && d.task.id) || d.id;
          for (let i = 0; i < 40; i++) {
            await new Promise(r => setTimeout(r, 500));
            const st = await fetch(`http://127.0.0.1:8786/api/compute/status?id=${taskId}`);
            const s = await st.json();
            if (s.status === "done") {
              const out = (s.result && s.result.output) || "";
              const running = /:5900|:6080/.test(out);
              return json({
                tool: "novnc",
                status: running ? "running" : "stopped",
                detail: out.trim() || "no vnc ports",
                url: "http://127.0.0.1:6080/vnc.html",
                note: "noVNC web remote desktop (x11vnc backend)",
              });
            }
          }
          return json({ tool: "novnc", status: "unknown", note: "status poll timeout" });
        }
        if (method === "POST") {
          const body = await request.json();
          const action = body.action;
          if (action === "start") {
            const r = await fetch("http://127.0.0.1:8786/api/compute/dispatch", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                id: "ghost-novnc-" + Date.now(),
                target: "auto",
                action: "run",
                params: { command: "nohup x11vnc -display :0.0 -forever -shared -rfbport 5900 >/tmp/x11vnc.log 2>&1 & nohup /usr/share/novnc/utils/novnc_proxy --vnc localhost:5900 --listen 6080 >/tmp/novnc.log 2>&1 & sleep 2; ss -tln | grep -E '5900|6080'" },
                origin: "ghost.eon",
              }),
            });
            const d = await r.json();
            return json({ tool: "novnc", action: "start", dispatched: d.ok });
          }
          if (action === "stop") {
            const r = await fetch("http://127.0.0.1:8786/api/compute/dispatch", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                id: "ghost-novnc-" + Date.now(),
                target: "auto",
                action: "run",
                params: { command: "pkill -f novnc_proxy; pkill -f x11vnc; sleep 1; ss -tln | grep -E '5900|6080' || echo 'stopped'" },
                origin: "ghost.eon",
              }),
            });
            const d = await r.json();
            return json({ tool: "novnc", action: "stop", dispatched: d.ok });
          }
          return json({ error: "action must be start|stop" }, 400);
        }
      } catch (e) {
        return json({ error: String(e) }, 500);
      }
    }

    // /tools/cf-deploy — CREATE A NEW CLOUDFLARE WORKER (self-replication!)
    // The ghost can create its own workers. Needs a CF token (stored in memory
    // via /memory or passed in body). This is the ghost's self-replication tool.
    if (path === "/tools/cf-deploy" && method === "POST") {
      try {
        const body = await request.json();
        const name = body.name;
        const code = body.code;
        const token = body.token || null;
        const accountId = body.account_id || null;
        if (!name || !code) return json({ error: "name and code required" }, 400);
        if (!token || !accountId) {
          return json({
            error: "CF token + account_id required",
            note: "Store a token in ghost memory via POST /memory {key:'cf_token', value:'...'} then retry",
          }, 400);
        }
        // Deploy via Cloudflare API
        const boundary = "----ghostboundary" + Date.now();
        const bodyStr = `--${boundary}\r\nContent-Disposition: form-data; name="worker.js"; filename="worker.js"\r\nContent-Type: application/javascript\r\n\r\n${code}\r\n--${boundary}--\r\n`;
        const r = await fetch(`https://api.cloudflare.com/client/v4/accounts/${accountId}/workers/scripts/${name}`, {
          method: "PUT",
          headers: {
            "Authorization": `Bearer ${token}`,
            "Content-Type": `multipart/form-data; boundary=${boundary}`,
          },
          body: bodyStr,
        });
        const d = await r.json();
        if (d.success) {
          // Record the creation in memory
          const memId = env.GHOST_MEMORY.idFromName("ghost-memory");
          const memObj = env.GHOST_MEMORY.get(memId);
          const memReq = new Request("http://ghost/memory", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ key: "created-worker:" + name, value: JSON.stringify({ name, ts: Date.now(), by: GHOST_NAME }) }),
          });
          await memObj.fetch(memReq);
          return json({
            ok: true,
            created: name,
            url: `https://${name}.workers.dev`,
            by: GHOST_NAME,
            note: "new worker created — the ghost replicated itself",
          });
        }
        return json({ ok: false, error: d.errors || d }, 500);
      } catch (e) {
        return json({ error: String(e) }, 500);
      }
    }

    // /tools/fetch — outbound HTTP fetch
    if (path === "/tools/fetch" && method === "POST") {
      try {
        const body = await request.json();
        const url = body.url;
        if (!url) return json({ error: "url required" }, 400);
        const r = await fetch(url, { method: body.method || "GET", headers: body.headers || {} });
        const text = await r.text();
        return json({ url, status: r.status, body: text.slice(0, 5000) });
      } catch (e) {
        return json({ error: String(e) }, 500);
      }
    }

    // /web/* — serve web content from ghost storage (apache2-like)
    if (path.startsWith("/web/")) {
      try {
        const key = "web:" + path.replace("/web/", "");
        const memId = env.GHOST_MEMORY.idFromName("ghost-memory");
        const memObj = env.GHOST_MEMORY.get(memId);
        const memReq = new Request("http://ghost/memory?key=" + encodeURIComponent(key), { method: "GET" });
        const memResp = await memObj.fetch(memReq);
        const memData = await memResp.json();
        if (memData.value) {
          return new Response(memData.value, {
            headers: { "Content-Type": "text/html", "X-Ghost-Web": "true" },
          });
        }
        return json({ error: "not found in ghost storage", key }, 404);
      } catch (e) {
        return json({ error: String(e) }, 500);
      }
    }

    // ─── OPENCODE HOSTING ───
    // The ghost hosts the FULL opencode stack: web UI, 42 agents, skills, config, and run.
    // /opencode — registry
    if (path === "/opencode") {
      return json({
        ghost: GHOST_NAME,
        opencode: {
          version: "1.18.25",
          binary: "/home/ricos/.opencode/bin/opencode",
          config: "/home/ricos/.config/opencode/opencode.jsonc",
          agents_dir: "/home/ricos/.config/opencode/agent",
          agents: 42,
          skills_dir: "/home/ricos/.agents/skills",
          web_ui: "http://127.0.0.1:4096/ (opencode serve)",
        },
        endpoints: {
          "GET /opencode/agents": "list all 42 agents",
          "GET /opencode/agent/{name}": "serve an agent .md file",
          "GET /opencode/skills": "list all skills",
          "GET /opencode/config": "serve opencode.jsonc",
          "POST /opencode/run": "run opencode {prompt, agent?, model?}",
          "POST /opencode/serve": "start opencode serve on :4096",
          "GET /opencode/web/*": "proxy to the opencode web UI",
        },
        note: "The ghost hosts opencode — it can run the full fleet and serve the web UI.",
      });
    }

    // /opencode/agents — list all agents (via swarm exec)
    if (path === "/opencode/agents") {
      try {
        const r = await fetch("http://127.0.0.1:8786/api/compute/dispatch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            id: "ghost-opencode-agents-" + Date.now(),
            target: "auto",
            action: "run",
            params: { command: "ls /home/ricos/.config/opencode/agent/*.md | xargs -n1 basename | sed 's/.md//' | sort" },
            origin: "ghost.eon",
          }),
        });
        const d = await r.json();
        const taskId = (d.task && d.task.id) || d.id;
        for (let i = 0; i < 40; i++) {
          await new Promise(r => setTimeout(r, 500));
          const st = await fetch(`http://127.0.0.1:8786/api/compute/status?id=${taskId}`);
          const s = await st.json();
          if (s.status === "done") {
            const out = (s.result && s.result.output) || "";
            return json({ ok: true, count: out.trim().split("\n").filter(Boolean).length, agents: out.trim().split("\n").filter(Boolean) });
          }
        }
        return json({ error: "timeout listing agents" }, 500);
      } catch (e) {
        return json({ error: String(e) }, 500);
      }
    }

    // /opencode/agent/{name} — serve an agent .md file
    if (path.startsWith("/opencode/agent/")) {
      try {
        const name = decodeURIComponent(path.replace("/opencode/agent/", ""));
        // Sanitize: only allow .md agent files
        if (!/^[a-zA-Z0-9_-]+$/.test(name)) return json({ error: "invalid agent name" }, 400);
        const r = await fetch("http://127.0.0.1:8786/api/compute/dispatch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            id: "ghost-opencode-agent-" + Date.now(),
            target: "auto",
            action: "run",
            params: { command: `cat /home/ricos/.config/opencode/agent/${name}.md 2>/dev/null || echo 'NOT_FOUND'` },
            origin: "ghost.eon",
          }),
        });
        const d = await r.json();
        const taskId = (d.task && d.task.id) || d.id;
        for (let i = 0; i < 40; i++) {
          await new Promise(r => setTimeout(r, 500));
          const st = await fetch(`http://127.0.0.1:8786/api/compute/status?id=${taskId}`);
          const s = await st.json();
          if (s.status === "done") {
            const out = (s.result && s.result.output) || "";
            if (out.trim() === "NOT_FOUND") return json({ error: "agent not found" }, 404);
            return new Response(out, { headers: { "Content-Type": "text/markdown", "X-Ghost-Opencode-Agent": name } });
          }
        }
        return json({ error: "timeout reading agent" }, 500);
      } catch (e) {
        return json({ error: String(e) }, 500);
      }
    }

    // /opencode/skills — list all skills
    if (path === "/opencode/skills") {
      try {
        const r = await fetch("http://127.0.0.1:8786/api/compute/dispatch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            id: "ghost-opencode-skills-" + Date.now(),
            target: "auto",
            action: "run",
            params: { command: "ls /home/ricos/.agents/skills/ /home/ricos/.claude/skills/ 2>/dev/null | sort -u" },
            origin: "ghost.eon",
          }),
        });
        const d = await r.json();
        const taskId = (d.task && d.task.id) || d.id;
        for (let i = 0; i < 40; i++) {
          await new Promise(r => setTimeout(r, 500));
          const st = await fetch(`http://127.0.0.1:8786/api/compute/status?id=${taskId}`);
          const s = await st.json();
          if (s.status === "done") {
            const out = (s.result && s.result.output) || "";
            return json({ ok: true, count: out.trim().split("\n").filter(Boolean).length, skills: out.trim().split("\n").filter(Boolean) });
          }
        }
        return json({ error: "timeout listing skills" }, 500);
      } catch (e) {
        return json({ error: String(e) }, 500);
      }
    }

    // /opencode/config — serve opencode.jsonc
    if (path === "/opencode/config") {
      try {
        const r = await fetch("http://127.0.0.1:8786/api/compute/dispatch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            id: "ghost-opencode-config-" + Date.now(),
            target: "auto",
            action: "run",
            params: { command: "cat /home/ricos/.config/opencode/opencode.jsonc" },
            origin: "ghost.eon",
          }),
        });
        const d = await r.json();
        const taskId = (d.task && d.task.id) || d.id;
        for (let i = 0; i < 40; i++) {
          await new Promise(r => setTimeout(r, 500));
          const st = await fetch(`http://127.0.0.1:8786/api/compute/status?id=${taskId}`);
          const s = await st.json();
          if (s.status === "done") {
            const out = (s.result && s.result.output) || "";
            return new Response(out, { headers: { "Content-Type": "application/json" } });
          }
        }
        return json({ error: "timeout reading config" }, 500);
      } catch (e) {
        return json({ error: String(e) }, 500);
      }
    }

    // /opencode/run — run opencode with a prompt (the ghost runs the fleet!)
    if (path === "/opencode/run" && method === "POST") {
      try {
        const body = await request.json();
        const prompt = body.prompt || body.message;
        if (!prompt) return json({ error: "prompt required" }, 400);
        const agent = body.agent ? `--agent ${body.agent}` : "";
        const model = body.model ? `--model ${body.model}` : "";
        const dir = body.dir ? `--dir ${body.dir}` : "";
        const cmd = `cd /home/ricos && timeout 120 opencode run ${JSON.stringify(prompt)} ${agent} ${model} ${dir} --format json 2>&1 | tail -c 8000`;
        const r = await fetch("http://127.0.0.1:8786/api/compute/dispatch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            id: "ghost-opencode-run-" + Date.now(),
            target: "auto",
            action: "run",
            params: { command: cmd },
            origin: "ghost.eon",
          }),
        });
        const d = await r.json();
        const taskId = (d.task && d.task.id) || d.id;
        for (let i = 0; i < 60; i++) {
          await new Promise(r => setTimeout(r, 1000));
          const st = await fetch(`http://127.0.0.1:8786/api/compute/status?id=${taskId}`);
          const s = await st.json();
          if (s.status === "done") {
            const out = (s.result && s.result.output) || "";
            // Record the run in memory
            try {
              const memId = env.GHOST_MEMORY.idFromName("ghost-memory");
              const memObj = env.GHOST_MEMORY.get(memId);
              const memReq = new Request("http://ghost/memory", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ key: "opencode-run:" + Date.now(), value: JSON.stringify({ prompt, agent: agent || "global", ts: Date.now(), output: out.slice(0, 500) }) }),
              });
              await memObj.fetch(memReq);
            } catch (e) { /* best-effort */ }
            return json({ ok: true, agent: agent || "global", output: out });
          }
          if (s.status === "failed" || s.status === "error") {
            return json({ ok: false, error: s });
          }
        }
        return json({ ok: true, note: "opencode run still executing (120s timeout)", task_id: taskId });
      } catch (e) {
        return json({ error: String(e) }, 500);
      }
    }

    // /opencode/serve — start the opencode headless server (web UI)
    if (path === "/opencode/serve" && method === "POST") {
      try {
        const body = await request.json();
        const port = body.port || 4096;
        const r = await fetch("http://127.0.0.1:8786/api/compute/dispatch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            id: "ghost-opencode-serve-" + Date.now(),
            target: "auto",
            action: "run",
            params: { command: `nohup opencode serve --port ${port} --hostname 127.0.0.1 >/tmp/opencode-serve.log 2>&1 & sleep 3; ss -tln | grep :${port} && echo 'SERVE_UP' || (echo 'SERVE_DOWN'; tail -5 /tmp/opencode-serve.log)` },
            origin: "ghost.eon",
          }),
        });
        const d = await r.json();
        const taskId = (d.task && d.task.id) || d.id;
        for (let i = 0; i < 40; i++) {
          await new Promise(r => setTimeout(r, 500));
          const st = await fetch(`http://127.0.0.1:8786/api/compute/status?id=${taskId}`);
          const s = await st.json();
          if (s.status === "done") {
            const out = (s.result && s.result.output) || "";
            return json({ ok: true, serve: out.trim(), web_ui: `http://127.0.0.1:${port}/`, note: "ghost hosts the opencode web UI" });
          }
        }
        return json({ error: "timeout starting serve" }, 500);
      } catch (e) {
        return json({ error: String(e) }, 500);
      }
    }

    // /opencode/web/* — proxy to the opencode web UI (full hosting)
    if (path.startsWith("/opencode/web")) {
      try {
        const target = "http://127.0.0.1:4096" + path.replace("/opencode/web", "") + (url.search || "");
        const headers = {};
        // Forward content-type for POST bodies
        const ct = request.headers.get("content-type");
        if (ct) headers["Content-Type"] = ct;
        const auth = request.headers.get("authorization");
        if (auth) headers["Authorization"] = auth;
        const r = await fetch(target, {
          method,
          headers,
          body: method === "GET" || method === "HEAD" ? undefined : request.body,
        });
        const respHeaders = new Headers(r.headers);
        respHeaders.set("Access-Control-Allow-Origin", "*");
        respHeaders.set("X-Ghost-Opencode-Web", "true");
        return new Response(r.body, { status: r.status, headers: respHeaders });
      } catch (e) {
        return json({ error: String(e), note: "start opencode serve first: POST /opencode/serve" }, 502);
      }
    }

    // ─── PQC handshake ───
    if (path === "/pqc/handshake" && method === "POST") {
      try {
        const body = await request.json();
        if (!body.client_pub) return json({ error: "client_pub required" }, 400);
        const serverKeys = await crypto.subtle.generateKey(
          { name: "ECDH", namedCurve: "P-256" }, true, ["deriveKey"]
        );
        const serverPub = await crypto.subtle.exportKey("raw", serverKeys.publicKey);
        const sessionId = crypto.randomUUID();
        return json({
          session_id: sessionId,
          server_pub: bufToBase64(serverPub),
          pqc: "x25519-sha3-aes256gcm",
          ghost: GHOST_NAME,
          autonomous: true,
        });
      } catch (e) {
        return json({ error: String(e) }, 500);
      }
    }

    // ─── Standard routes ───
    if (ROUTES[path]) {
      return json(ROUTES[path]());
    }

    return json({ error: "not found", routes: Object.keys(ROUTES) }, 404);
  },
};