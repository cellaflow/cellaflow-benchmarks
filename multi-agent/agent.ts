// One agent. Reads a ticket, decides a refund amount, issues it.
//
// The two agents in this benchmark are given different amounts deliberately.
// That is the whole scenario: they are not retries of one another, they are
// independent reasoners that reached different conclusions about the same work.
// Real agents do this because the model is non-deterministic, not because
// anything went wrong.
import { durableTools, tool, IdempotencyScope } from "@cellaflow/sdk";
import { refund } from "./gateway.js";

const RUN = process.env.BENCH_RUN!;
const TICKET = process.env.BENCH_TICKET!;
const AMOUNT = Number(process.env.BENCH_AMOUNT!);
const AGENT = process.env.BENCH_AGENT!;
const ARM = process.env.BENCH_ARM!;
const TARGET = process.env.CELLAFLOW_TARGET ?? "localhost:50051";

/** The irreversible act. Money leaves the account. */
function issueRefund(args: { ticket: string; amount: number }): { refunded: number } {
  refund({ run: RUN, ticket: args.ticket, amount: args.amount, agent: AGENT });
  return { refunded: args.amount };
}

async function main() {
  if (ARM === "none") {
    // No coordination at all. Each agent acts on its own conclusion.
    const r = issueRefund({ ticket: TICKET, amount: AMOUNT });
    console.log(`[${AGENT}] refunded ${r.refunded}`);
    return;
  }

  // Both guarded arms use the same session-per-agent shape, because that is
  // what two independently spawned agents actually have. They are not two
  // workers inside one run.
  const sessionThread = `${RUN}-${AGENT}`;

  if (ARM === "hash-args") {
    // The obvious guard: an idempotency key derived from what the tool was
    // called with. This is what a team writes first, and what most libraries
    // give you by default.
    const guarded = tool(issueRefund, {
      toolName: "issue_refund",
      scope: IdempotencyScope.SHARED,
    });
    await durableTools(sessionThread, { coordinationId: TICKET, target: TARGET }, async () => {
      const r = await guarded({ ticket: TICKET, amount: AMOUNT });
      console.log(`[${AGENT}] result ${JSON.stringify(r)}`);
    });
    return;
  }

  if (ARM === "shared-on") {
    // Key derived from the business fact alone. The amount is deliberately not
    // hashed, so two agents who disagree about it still derive one key.
    const guarded = tool(issueRefund, {
      toolName: "issue_refund",
      scope: IdempotencyScope.SHARED,
      sharedOn: ["ticket"],
    });
    await durableTools(sessionThread, { coordinationId: TICKET, target: TARGET }, async () => {
      const r = await guarded({ ticket: TICKET, amount: AMOUNT });
      console.log(`[${AGENT}] result ${JSON.stringify(r)}`);
    });
    return;
  }

  throw new Error(`unknown arm: ${ARM}`);
}

main().catch((err) => {
  console.error(`[${AGENT}] failed:`, err?.message ?? err);
  process.exit(1);
});
