// The CellaFlow arm, as a leasing proxy in front of the model endpoint.
//
// Nothing about Inconvo's graph or its sub-agents is edited or reimplemented.
// The audit driver already points OPENAI_BASE_URL at a fake provider, so this
// arm points it at this proxy instead, and the proxy forwards to that same fake
// provider. Everything the audit measures still happens; the only difference is
// what sits in between.
//
// Each forwarded model call is wrapped in `tool()`, which does the three things
// a checkpointer does not: it admits one caller, it commits what that caller
// received, and it hands that record to whoever asks next. The third is the one
// that matters here, because Inconvo's MemorySaver is per process and dies with
// it, so a retry has nothing to be handed.
import { createServer } from "node:http";
import { createHash } from "node:crypto";
import { durableTools, tool } from "@cellaflow/sdk";

const PORT = Number(process.env.BENCH_PROXY_PORT ?? 8318);
const UPSTREAM = process.env.BENCH_UPSTREAM ?? "http://127.0.0.1:8317";
const TARGET = process.env.CELLAFLOW_TARGET ?? "localhost:50051";

// Set by the harness before each attempt. The thread is the conversation, so
// attempt 1 and its retry share it: that is what lets the retry be handed what
// attempt 1 already paid for.
let thread = "unknown";

// Why each model call gets its own session.
//
// The first version opened one session per HTTP request, and every call then
// claimed sequence 1 of the same session: the engine refused the second call of
// an answer as a divergent step. The second version held one session open for
// the whole attempt and bound each request onto it, which surfaced the real
// shape of this graph: Inconvo fans out concurrent model calls, and three in
// flight against one shared sequence counter produced "expected sequence 7, but
// request specified 9".
//
// Both attempts were forcing a position on operations that do not have one. A
// sequence claim answers which agent owns a graph position; these calls are
// independent and concurrent, so the only question that matters is the
// cache's: has this exact call already been made? So each call is its own
// single-step session, keyed on the request, and the position never collides.
// The idempotency cache is position-independent and cross-session by design,
// which is what makes the retry a hit.

/**
 * The identity of one model call.
 *
 * Derived from the request body rather than from a counter, because a counter
 * is position-dependent and a crashed attempt does not resume at the position
 * it died at. Two attempts asking the same question of the same model with the
 * same messages are the same operation, wherever they fall in the sequence.
 */
function digestOf(body: unknown): string {
  return createHash("sha256")
    .update(JSON.stringify(body ?? {}))
    .digest("hex")
    .slice(0, 32);
}

function readBody(req: any): Promise<string> {
  return new Promise((resolve) => {
    let data = "";
    req.on("data", (c: Buffer) => (data += c));
    req.on("end", () => resolve(data));
  });
}

/** Forwards one call upstream. Wrapped below, so a second caller deriving the
 *  same key never enters this body and is handed what the first one got. */
async function callUpstream(path: string, body: unknown): Promise<unknown> {
  const res = await fetch(`${UPSTREAM}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

const server = createServer(async (req, res) => {
  const raw = await readBody(req);

  if (req.url === "/control/thread" && req.method === "POST") {
    thread = String(JSON.parse(raw || "{}").thread ?? "unknown");
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ thread }));
    return;
  }

  if (req.url === "/control/health") {
    res.writeHead(200).end("ok");
    return;
  }

  let body: unknown = {};
  try {
    body = JSON.parse(raw || "{}");
  } catch {
    /* forwarded as an empty body; upstream tolerates it */
  }

  const path = req.url ?? "/v1/responses";
  const digest = digestOf(body);
  const leased = tool(async () => callUpstream(path, body), {
    idempotencyKey: `inconvo:model:${thread}:${digest}`,
    toolName: "model_call",
  });

  try {
    // Session id carries the thread and the request, so attempt 1 and its retry
    // land on the same session while two different calls never share one.
    const reply = await durableTools(
      `inconvo:${thread}:${digest}`,
      { target: TARGET },
      () => leased(),
    );
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(reply));
  } catch (err: any) {
    // "fetch failed" here is the crash itself: the provider was holding this
    // call open when the driver was killed, and the socket is released when the
    // next attempt starts. Anything else is a real fault, and a proxy that
    // failed silently would look like a cheaper arm rather than a broken one.
    const expected = String(err?.message ?? err).includes("fetch failed");
    console.error(expected ? "  (call lost to the crash, as expected)" : `PROXY_ERROR ${err?.message ?? err}`);
    res.writeHead(502, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: String(err?.message ?? err) }));
  }
});

server.listen(PORT, "127.0.0.1", () => {
  console.error(`leasing proxy listening on 127.0.0.1:${PORT} -> ${UPSTREAM}`);
});
