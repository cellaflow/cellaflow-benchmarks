// One answer, through Inconvo's real graph.
//
// Nothing about the agent tree is reimplemented: `inconvoAgent` is imported
// from the clone and invoked as the platform invokes it. The harness supplies
// only the two things a benchmark must, and says so in the README: the model
// endpoint, and the database connector.
//
// This process does not decide when to crash. The harness watches the ledger
// and kills it, so the crash point is defined by observed model calls rather
// than by anything the driver reports about itself.
import { AGENTS_SRC, LANGGRAPH, PROVIDER_URL } from "./config.ts";
import { QUESTION, SCHEMA, fixtureConnector } from "./fixture.ts";

const RUN = process.env.BENCH_RUN!;
const THREAD = process.env.BENCH_THREAD!;

// Redirects every getAIModel() call site at the fake provider. Inconvo is not
// edited to make this work; ChatOpenAI honours the OpenAI SDK's base URL.
process.env.OPENAI_BASE_URL = PROVIDER_URL;
process.env.OPENAI_API_KEY = "bench-not-a-real-key";

async function main() {
  const { inconvoAgent } = await import(`${AGENTS_SRC}/inconvo/index.ts`);
  const { MemorySaver } = await import(LANGGRAPH);

  // A MemorySaver per process is deliberate and is the point of the benchmark:
  // Inconvo's checkpointer is passed to the outermost graph only, so whatever
  // it preserves is all that survives. Every sub-agent is compiled without one.
  const checkpointer = new MemorySaver();

  const { graph } = await inconvoAgent({
    databases: [
      {
        friendlyName: "analytics",
        context: "Order and customer records.",
        schema: SCHEMA,
        connector: fixtureConnector(),
      },
    ],
    checkpointer,
    conversation: {
      id: THREAD,
      title: null,
      userIdentifier: "bench-user",
      userContext: null,
    },
    orgId: "bench-org",
    agentId: "bench-agent",
    runId: RUN,
    userIdentifier: "bench-user",
    provider: "openai",
  });

  const result = await graph.invoke(
    { userQuestion: QUESTION, runId: RUN },
    { configurable: { thread_id: THREAD }, recursionLimit: 50 },
  );

  console.log(JSON.stringify({ ok: true, answer: result?.answer ?? null }));
}

main().catch((e) => {
  console.error("DRIVER_ERROR", e?.message ?? e);
  process.exit(1);
});
