// Two agents, one ticket, different conclusions. What does each guard do?
//
// Each agent is its own OS process, because two independently spawned agents
// are what the scenario is about. They do not share memory, a session, or any
// channel to coordinate on.
import { spawn } from "node:child_process";
import { join } from "node:path";
import { refundsFor, reset } from "./gateway.js";

const HERE = import.meta.dirname;
const TSX = join(HERE, "node_modules/.bin/tsx");
const REPEATS = Number(process.env.BENCH_REPEATS ?? 5);

/** Agent A says 40, agent B says 35. Neither is wrong; they reasoned separately. */
const AMOUNTS: Record<string, number> = { "agent-a": 40, "agent-b": 35 };

type Trial = { refunds: number; total: number; amounts: number[] };

function runAgent(run: string, ticket: string, agent: string, arm: string): Promise<void> {
  return new Promise((resolve) => {
    const child = spawn(TSX, [join(HERE, "agent.ts")], {
      cwd: HERE,
      env: {
        ...process.env,
        BENCH_RUN: run,
        BENCH_TICKET: ticket,
        BENCH_AGENT: agent,
        BENCH_AMOUNT: String(AMOUNTS[agent]),
        BENCH_ARM: arm,
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let err = "";
    child.stderr.on("data", (d) => (err += d));
    const kill = setTimeout(() => child.kill("SIGKILL"), 60_000);
    child.on("close", (code) => {
      clearTimeout(kill);
      if (code !== 0) console.error(`  ${agent} exited ${code}: ${err.slice(0, 200)}`);
      resolve();
    });
  });
}

async function trial(arm: string): Promise<Trial> {
  const run = `run-${Math.random().toString(16).slice(2, 8)}`;
  const ticket = `TICKET-${run.slice(4)}`;
  // Genuinely concurrent. Serialising them would measure nothing.
  await Promise.all(["agent-a", "agent-b"].map((a) => runAgent(run, ticket, a, arm)));
  const rs = refundsFor(run);
  return {
    refunds: rs.length,
    total: rs.reduce((a, r) => a + r.amount, 0),
    amounts: rs.map((r) => r.amount),
  };
}

async function arm(name: string, id: string): Promise<void> {
  const trials: Trial[] = [];
  for (let i = 0; i < REPEATS; i++) trials.push(await trial(id));

  const avgRefunds = trials.reduce((a, t) => a + t.refunds, 0) / trials.length;
  const avgTotal = trials.reduce((a, t) => a + t.total, 0) / trials.length;
  const bothRan = trials.filter((t) => t.refunds > 1).length;

  // The customer is owed one refund. Which amount is a business question; two
  // refunds is not.
  const note =
    bothRan > 0
      ? `BOTH AGENTS REFUNDED, ${bothRan} of ${trials.length} runs`
      : "one refund, the disagreement never surfaced";

  console.log(
    `  ${name.padEnd(34)}${avgRefunds.toFixed(1).padStart(9)}${avgTotal.toFixed(0).padStart(9)}   ${note}`,
  );
}

async function main(): Promise<number> {
  reset();

  console.log();
  console.log("  Two agents, one ticket, different conclusions.");
  console.log("  Agent A decides the refund is 40. Agent B decides it is 35.");
  console.log("  Neither is retrying the other. They reasoned separately and disagree.");
  console.log("  The customer is owed one refund.");
  console.log();
  console.log(`  ${"guard".padEnd(34)}${"refunds".padStart(9)}${"paid".padStart(9)}   what happened`);
  console.log("  " + "-".repeat(84));

  await arm("no guard", "none");
  await arm("idempotency key on arguments", "hash-args");
  await arm("key on the business fact", "shared-on");

  console.log();
  console.log(`  ${REPEATS} runs per row. Correct is one refund.`);
  console.log();
  console.log("  Note what none of these rows says: which amount was right, or that");
  console.log("  anyone was told the agents disagreed. Deduplication answers neither.");
  console.log();
  return 0;
}

main().then((c) => process.exit(c));
