// The fake model endpoint, run as its own process.
//
// It must be a separate process, not a server started inside the harness:
// spawnSync in the harness blocks the event loop so an in-process provider can
// never answer, and it also serialises processes that are supposed to run
// concurrently. Async spawn plus Promise.all, provider standalone.
//
// Every agent in the tree reaches OpenAI through getAIModel(), which builds a
// ChatOpenAI with useResponsesApi: true. So this answers /v1/responses, and
// /v1/chat/completions as a fallback, and pointing OPENAI_BASE_URL here
// redirects all eleven call sites without editing Inconvo.
import { createHash } from "node:crypto";
import { appendFileSync } from "node:fs";
import { createServer } from "node:http";
import { join } from "node:path";
import { record } from "./ledger.ts";

const PORT = Number(process.env.BENCH_PROVIDER_PORT ?? 8317);
// Run and attempt are set by the harness before each attempt, not read from
// this process's own environment. The provider outlives every driver, so
// stamping its own startup env on each entry labelled every run identically
// and made attempt 1 indistinguishable from the retry.
let run = "unknown";
const DUMP = process.env.BENCH_DUMP === "1";
const DUMP_FILE = join(import.meta.dirname, "requests.jsonl");

// Set by the harness between attempts, so the ledger can tell the original
// answer from the retry without the agent tree being asked.
let attempt = 1;

// How many times each site has been called within the current attempt. The
// canned script is positional: an agent asked twice gets a different answer the
// second time, which is what makes a tool loop terminate instead of spinning.
const calls = new Map<string, number>();

/** Fingerprint a request by its system prompt: each of the eleven call sites
 *  carries a different one, so this names the site without Inconvo cooperating. */
function siteOf(body: any): string {
  const parts: string[] = [];
  const input = body?.input ?? body?.messages ?? [];
  for (const m of Array.isArray(input) ? input : []) {
    const role = m?.role ?? "";
    if (role === "system" || role === "developer") {
      const c = typeof m.content === "string"
        ? m.content
        : JSON.stringify(m.content ?? "");
      parts.push(c.slice(0, 400));
    }
  }
  if (body?.instructions) parts.push(String(body.instructions).slice(0, 400));
  const tools = (body?.tools ?? []).map((t: any) => t?.name ?? t?.function?.name).join(",");
  if (tools) parts.push("tools:" + tools);
  const digest = createHash("sha256").update(parts.join("|")).digest("hex").slice(0, 8);
  const label = tools ? tools.split(",")[0] : "chat";
  return `${label}:${digest}`;
}

function readBody(req: any): Promise<string> {
  return new Promise((resolve) => {
    let data = "";
    req.on("data", (c: Buffer) => (data += c));
    req.on("end", () => resolve(data));
  });
}

const server = createServer(async (req, res) => {
  const raw = await readBody(req);

  if (req.url === "/control/attempt" && req.method === "POST") {
    const body = JSON.parse(raw || "{}");
    if (body.run !== undefined) run = String(body.run);
    attempt = Number(body.attempt ?? 1);
    calls.clear();
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ run, attempt }));
    return;
  }

  if (req.url === "/control/health") {
    res.writeHead(200).end("ok");
    return;
  }

  let body: any = {};
  try {
    body = JSON.parse(raw || "{}");
  } catch {
    /* fall through with an empty body; siteOf tolerates it */
  }

  const site = siteOf(body);

  if (DUMP) {
    appendFileSync(DUMP_FILE, JSON.stringify({ url: req.url, site, body }) + "\n");
  }

  // Appended and fsynced before a byte of the response is written. A driver
  // killed the instant it receives this is still counted as having called.
  record({
    run,
    event: "model_call",
    site,
    attempt,
    promptTokens: JSON.stringify(body?.input ?? body?.messages ?? "").length >> 2,
    completionTokens: 0,
    at: Date.now(),
  });

  const reply = respond(body, req.url ?? "", site);
  res.writeHead(200, { "content-type": "application/json" });
  res.end(JSON.stringify(reply));
});

function toolNames(body: any): string[] {
  return (body?.tools ?? [])
    .map((t: any) => t?.name ?? t?.function?.name)
    .filter(Boolean);
}

function functionCall(name: string, args: unknown, model: string): any {
  return {
    id: "resp_bench",
    object: "response",
    created_at: Math.floor(Date.now() / 1000),
    model,
    status: "completed",
    output: [
      {
        type: "function_call",
        id: "fc_bench",
        call_id: `call_${Math.random().toString(16).slice(2, 10)}`,
        name,
        arguments: JSON.stringify(args),
        status: "completed",
      },
    ],
    usage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
  };
}

function text(t: string, model: string): any {
  return {
    id: "resp_bench",
    object: "response",
    created_at: Math.floor(Date.now() / 1000),
    model,
    status: "completed",
    output: [
      {
        type: "message",
        id: "msg_bench",
        role: "assistant",
        status: "completed",
        content: [{ type: "output_text", text: t, annotations: [] }],
      },
    ],
    usage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
  };
}

// A canned agent, not a model. Every answer is fixed, so the run is
// deterministic and costs nothing, and the only thing that varies between the
// arms is how much of this script has to be replayed after a crash.
function respond(body: any, url: string, site: string): any {
  const model = body?.model ?? "gpt-bench";
  const nth = (calls.get(site) ?? 0) + 1;
  calls.set(site, nth);
  const tools = toolNames(body);

  if (!url.includes("/responses")) {
    return {
      id: "chatcmpl_bench",
      object: "chat.completion",
      created: Math.floor(Date.now() / 1000),
      model,
      choices: [
        { index: 0, message: { role: "assistant", content: "{}" }, finish_reason: "stop" },
      ],
      usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
    };
  }

  // The outermost agent. First it fetches data, which is what descends into the
  // sub-agent tree; then it formats an answer and stops.
  if (tools.includes("databaseRetriever")) {
    if (nth === 1) {
      return functionCall(
        "databaseRetriever",
        {
          database: "analytics",
          query: {
            table: "orders",
            operation: "aggregateGroups",
            operationParameters: {},
            questionConditions: null,
          },
        },
        model,
      );
    }
    if (nth === 2 && tools.includes("generateResponse")) {
      return functionCall(
        "generateResponse",
        { code: "print(__import__('json').dumps({'type':'text','message':'Hardware led on revenue.'}))" },
        model,
      );
    }
    return text("Hardware led on revenue.", model);
  }

  // Any sub-agent: answer its first offered tool once, then fall through to
  // text so its loop terminates.
  if (tools.length > 0 && nth === 1) {
    return functionCall(tools[0], {}, model);
  }
  return text("{}", model);
}

server.listen(PORT, "127.0.0.1", () => {
  console.error(`provider listening on 127.0.0.1:${PORT}`);
});
