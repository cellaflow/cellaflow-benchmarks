// Where the Inconvo clone lives. The benchmark imports their real agent graph
// from here; nothing about the graph or its sub-agents is reimplemented.
//
//   git clone https://github.com/inconvo/inconvo
//   cd inconvo && git checkout fa63f29
//   corepack pnpm install --filter "@repo/agents..."
//
// Their package.json requires node >= 22 and pins pnpm 11.1.2.
export const INCONVO_SRC =
  "/Users/blade/theblueskies/startup-source-codes/inconvo";

export const AGENTS_SRC = `${INCONVO_SRC}/packages/agents/src`;

// LangGraph is resolved out of Inconvo's own tree rather than installed here,
// so the benchmark runs against the exact version they run (1.3.0) and cannot
// drift onto a different one.
export const LANGGRAPH =
  `${INCONVO_SRC}/packages/agents/node_modules/@langchain/langgraph/dist/index.js`;

// The fake model endpoint. Every agent in the tree reaches OpenAI through
// getAIModel(), which constructs ChatOpenAI, so pointing the OpenAI SDK at a
// local base URL redirects all eleven call sites without touching their code.
export const PROVIDER_URL =
  process.env.BENCH_PROVIDER_URL ?? "http://127.0.0.1:8317/v1";
