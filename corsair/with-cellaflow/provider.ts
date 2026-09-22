// A fake OAuth provider that rotates its refresh token, which is what Microsoft,
// Google and Salesforce do. Rotation is the whole reason a concurrent refresh is
// dangerous rather than merely wasteful: each successful refresh consumes the
// token presented and issues a new one, so presenting a consumed token returns
// invalid_grant exactly as a real provider does.
//
// Runs as its own process. The harness spawns drivers synchronously, which would
// block an in-process server's event loop and deadlock every refresh.
import { createServer } from "node:http";
import { PROVIDER_PORT } from "./config.js";
import { record } from "./ledger.js";

/** run id -> the one refresh token currently considered valid. */
const valid = new Map<string, string>();
/** run id -> the token just superseded, honoured only in grace mode. */
const previous = new Map<string, string>();
/** run ids whose provider keeps the superseded token briefly usable. */
const graceRuns = new Set<string>();
let issued = 0;

const readBody = (req: any): Promise<string> =>
  new Promise((resolve) => {
    let b = "";
    req.on("data", (c: string) => (b += c));
    req.on("end", () => resolve(b));
  });

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", "http://x");

  if (url.pathname === "/health") {
    res.writeHead(200).end("ok");
    return;
  }

  // Adjudication: which refresh token would still work right now.
  if (url.pathname === "/valid") {
    const run = url.searchParams.get("run") ?? "";
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ valid: valid.get(run) ?? null }));
    return;
  }

  if (url.pathname === "/seed") {
    const params = new URLSearchParams(await readBody(req));
    const run = params.get("run") ?? "";
    valid.set(run, params.get("token") ?? "");
    previous.delete(run);
    if (params.get("grace") === "1") graceRuns.add(run);
    else graceRuns.delete(run);
    res.writeHead(200).end("seeded");
    return;
  }

  if (url.pathname === "/token") {
    const params = new URLSearchParams(await readBody(req));
    const run = params.get("run") ?? "";
    const presented = params.get("refresh_token") ?? "";

    // Grace mode models Microsoft, Auth0 and Okta, which keep the superseded
    // refresh token usable for a short window so that a racing client is not
    // punished for a network retry. It is the setting under which two
    // processes can BOTH succeed, which is where a lost update does damage.
    const acceptable =
      valid.get(run) === presented ||
      (graceRuns.has(run) && previous.get(run) === presented);

    if (!acceptable) {
      // Already spent by another process, or never issued.
      record({ run, event: "refresh_rejected", presented, at: Date.now() });
      res.writeHead(400, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "invalid_grant" }));
      return;
    }

    const next = `r${++issued}`;
    previous.set(run, valid.get(run) ?? presented);
    valid.set(run, next);
    record({ run, event: "refresh_accepted", presented, issued: next, at: Date.now() });
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ access_token: `a${issued}`, refresh_token: next, expires_in: 3600 }));
    return;
  }

  res.writeHead(404).end();
});

server.listen(PROVIDER_PORT, () => {
  if (process.env.BENCH_DEBUG) console.error(`[provider] listening on ${PROVIDER_PORT}`);
});
