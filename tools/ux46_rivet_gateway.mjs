#!/usr/bin/env node
/**
 * A persistent OpenClaw Gateway connection for the UX46 Agent3 adapter.
 *
 * This runs on the machine that already hosts the gateway, beside the running
 * `openclaw` install, and speaks newline-delimited JSON on stdin/stdout to its
 * Python parent (tools/atlas_rivet.py).
 *
 * Two boundaries this file exists to keep:
 *
 *   1. Authentication is resolved *inside this process* by OpenClaw's own
 *      published SDK entry point (`openclaw/plugin-sdk/gateway-runtime`). The
 *      token or password never reaches the parent process, the HTTP surface,
 *      the wire to any other machine, or a log line here. Nothing in this file
 *      reads a credential field by name or prints one.
 *
 *   2. Only the method names the parent is allowed to ask for are forwarded,
 *      and that list is enforced here as well as in the parent, so a bug in
 *      one layer is not an arbitrary-RPC hole in the gateway.
 *
 * Run it directly to check the connection without a browser:
 *
 *     node tools/ux46_rivet_gateway.mjs --probe
 */

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import readline from "node:readline";
import { pathToFileURL } from "node:url";

const ALLOWED_METHODS = new Set([
  "agent.identity.get",
  "agents.list",
  "sessions.list",
  // Creating one fresh conversation for /new. No task, message, parent, fork
  // or worktree is ever forwarded with it, so no model run starts.
  "sessions.create",
  "sessions.patch",
  "sessions.compact",
  "chat.history",
  "chat.send",
  "chat.abort",
  "sessions.messages.subscribe",
  "sessions.messages.unsubscribe",
]);

// Events this bridge relays upward. Anything else the gateway broadcasts is
// dropped rather than forwarded to a browser-facing surface.
const RELAYED_EVENTS = new Set(["session.message", "sessions.changed"]);

const DEFAULT_PORT = 18790;

function parseArgs(argv) {
  const opts = {
    probe: false,
    url: "",
    openclawRoot: "",
    clientDisplayName: "UX46 Agent3 adapter",
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--probe") opts.probe = true;
    else if (arg === "--url") opts.url = String(argv[++i] ?? "");
    else if (arg === "--openclaw-root") opts.openclawRoot = String(argv[++i] ?? "");
    else if (arg === "--client-display-name") opts.clientDisplayName = String(argv[++i] ?? "");
  }
  return opts;
}

/**
 * Find the installed OpenClaw package.
 *
 * A global `npm install -g openclaw` is not resolvable from an arbitrary
 * working directory, so a bare import is tried first and the package root is
 * otherwise derived from the `openclaw` executable already on PATH. Nothing is
 * downloaded, installed or written.
 */
function findOpenClawRoot(explicit) {
  if (explicit) return path.resolve(explicit);
  for (const dir of String(process.env.PATH || "").split(path.delimiter)) {
    if (!dir) continue;
    const candidate = path.join(dir, "openclaw");
    let entry;
    try {
      entry = fs.realpathSync(candidate);
    } catch {
      continue;
    }
    // .../openclaw/dist/index.js -> .../openclaw
    let root = path.dirname(entry);
    while (root !== path.dirname(root)) {
      if (fs.existsSync(path.join(root, "package.json"))) return root;
      root = path.dirname(root);
    }
  }
  return "";
}

/** Import one OpenClaw entry point, by package name or by installed path. */
async function importOpenClaw(subpath, root) {
  const bare = subpath ? `openclaw/${subpath}` : "openclaw";
  try {
    return await import(bare);
  } catch (bareError) {
    if (!root) throw bareError;
    const file = subpath
      ? path.join(root, "dist", `${subpath}.js`)
      : path.join(root, "dist", "index.js");
    return await import(pathToFileURL(file).href);
  }
}

/** One line of JSON on stdout. Never called with anything auth-derived. */
function emit(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

/**
 * Build a connected client using OpenClaw's own configuration and auth.
 *
 * `resolveGatewayAuth` returns the effective mode plus whichever secret that
 * mode needs; it is handed straight to `GatewayClient` and is never read,
 * copied, compared or reported by this file.
 */
async function connect(opts, onEvent) {
  const root = findOpenClawRoot(opts.openclawRoot);
  const { GatewayClient, resolveGatewayAuth } = await importOpenClaw(
    "plugin-sdk/gateway-runtime", root,
  );
  const { loadConfig } = await importOpenClaw("", root);
  const cfg = await loadConfig();
  const auth = resolveGatewayAuth({ authConfig: cfg?.gateway?.auth });
  const port = Number(cfg?.gateway?.port) || DEFAULT_PORT;
  const url = opts.url || `ws://127.0.0.1:${port}`;

  let helloResolve;
  let helloReject;
  const hello = new Promise((resolve, reject) => {
    helloResolve = resolve;
    helloReject = reject;
  });

  const client = new GatewayClient({
    url,
    ...(auth.token ? { token: auth.token } : {}),
    ...(auth.password ? { password: auth.password } : {}),
    clientName: "gateway-client",
    clientDisplayName: opts.clientDisplayName,
    mode: "backend",
    onHelloOk: (frame) => helloResolve(frame),
    onConnectError: (err) => helloReject(err),
    onEvent: (frame) => {
      if (!frame || typeof frame !== "object") return;
      const name = String(frame.method ?? frame.event ?? "");
      if (!RELAYED_EVENTS.has(name)) return;
      onEvent(name, frame.params ?? frame.payload ?? {});
    },
    onClose: (code, reason) => {
      // The client reconnects on its own; the parent is told so it can report
      // a degraded stream instead of silently showing stale history.
      emit({ type: "transport", state: "disconnected", code, reason: String(reason || "") });
    },
  });
  client.start();
  const helloFrame = await hello;
  return { client, url, authMode: auth.mode, hello: helloFrame, root };
}

/** What the parent may know about the connection: never a credential. */
function describe(state) {
  return {
    url: state.url,
    // The *mode*, so an operator can see whether the gateway wanted auth at
    // all. Never the token or password that satisfied it.
    auth_mode: state.authMode,
    gateway_version: String(state.hello?.self?.version ?? state.hello?.version ?? ""),
    instance_id: String(state.hello?.self?.instanceId ?? ""),
    scopes: Array.isArray(state.hello?.auth?.scopes) ? state.hello.auth.scopes : [],
    role: String(state.hello?.auth?.role ?? ""),
    openclaw_root: state.root || "(resolved by package name)",
  };
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  let state;
  try {
    state = await connect(opts, (event, payload) => emit({ type: "event", event, payload }));
  } catch (err) {
    emit({
      type: "ready",
      ok: false,
      error: { code: "gateway_unreachable", message: String(err?.message || err) },
    });
    process.exitCode = 1;
    return;
  }

  emit({ type: "ready", ok: true, gateway: describe(state) });
  if (opts.probe) {
    await state.client.stopAndWait({ timeoutMs: 2000 }).catch(() => {});
    return;
  }

  const rl = readline.createInterface({ input: process.stdin });
  rl.on("line", async (line) => {
    const text = line.trim();
    if (!text) return;
    let req;
    try {
      req = JSON.parse(text);
    } catch {
      emit({ type: "response", id: null, ok: false,
             error: { code: "bad_request", message: "the bridge was sent malformed JSON" } });
      return;
    }
    const id = req?.id ?? null;
    const method = String(req?.method ?? "");
    if (!ALLOWED_METHODS.has(method)) {
      emit({ type: "response", id, ok: false,
             error: { code: "method_not_allowed",
                      message: `this bridge never forwards ${method || "(no method)"}` } });
      return;
    }
    const timeoutMs = Number.isFinite(req?.timeout_ms) ? Number(req.timeout_ms) : 20000;
    try {
      const result = await state.client.request(method, req?.params ?? {}, { timeoutMs });
      emit({ type: "response", id, ok: true, result });
    } catch (err) {
      // The parent decides what an error means for the person; the bridge only
      // reports it verbatim and never retries a write on its own.
      emit({ type: "response", id, ok: false,
             error: { code: String(err?.code || "gateway_error"),
                      message: String(err?.message || err) } });
    }
  });
  rl.on("close", async () => {
    await state.client.stopAndWait({ timeoutMs: 2000 }).catch(() => {});
    process.exit(0);
  });
}

main().catch((err) => {
  emit({ type: "ready", ok: false,
         error: { code: "bridge_failed", message: String(err?.message || err) } });
  process.exitCode = 1;
});
