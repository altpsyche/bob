import { spawn } from "node:child_process";
import z from "@deepseek-ai/schemastery";

export const name = "bob-dsh-bridge";

export const Config = z.object({
  bobCommand: z.string().default("bob"),
  importArgs: z.array(z.string()).default(["dsh", "import-session", "--stdin"]),
  timeoutMs: z.number().step(1).min(1000).default(30000),
});

function collectSessions(ctx, rootId) {
  const all = typeof ctx.sessions?.list === "function" ? ctx.sessions.list() : [];
  const byParent = new Map();
  for (const session of all) {
    const parent = session?.header?.parentSession;
    if (!parent) continue;
    if (!byParent.has(parent)) byParent.set(parent, []);
    byParent.get(parent).push(session);
  }
  const out = [];
  const seen = new Set();
  const visit = (session) => {
    const id = session?.header?.id;
    if (!id || seen.has(id)) return;
    seen.add(id);
    out.push(session);
    for (const child of byParent.get(id) ?? []) visit(child);
  };
  const root = all.find((session) => session?.header?.id === rootId);
  if (root) visit(root);
  return out;
}

function runBob(command, args, payload, timeoutMs) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const child = spawn(command, args, { stdio: ["pipe", "pipe", "pipe"] });
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill();
      reject(new Error(`bob ${args.join(" ")} timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    let stderr = "";
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    child.on("error", (err) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(err);
    });
    child.on("close", (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (code === 0) resolve();
      else reject(new Error(stderr.trim() || `bob import exited ${code}`));
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

export function apply(ctx, config) {
  const resolved = config ?? {};
  const command = resolved.bobCommand ?? "bob";
  const args = resolved.importArgs ?? ["dsh", "import-session", "--stdin"];
  const timeoutMs = resolved.timeoutMs ?? 30000;

  ctx.on("agent/turn-stopping", async ({ agent }) => {
    const rootId = agent?.session?.header?.id;
    if (!rootId) return;
    try {
      const sessions = collectSessions(ctx, rootId).map((session) => ({
        session_id: session.header.id,
        parent_session_id: session.header.parentSession ?? null,
        cwd: session.header.cwd ?? null,
        origin: session.header.origin ?? null,
        events: typeof session.snapshotEvents === "function" ? session.snapshotEvents() : [],
        messages: typeof session.deriveMessages === "function" ? session.deriveMessages() : [],
      }));
      await runBob(command, args, {
        root_session_id: rootId,
        source: "dsh",
        sessions,
      }, timeoutMs);
    } catch (err) {
      ctx.logger?.warn?.("bob-dsh-bridge import failed: %s", err?.message ?? err);
    }
  });
}
