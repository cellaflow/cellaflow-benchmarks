// Runs every scenario against Inconvo's real agent graph and prints the table.
//
// Each attempt spawns `driver.ts` as its own OS process, so "a crash" means a
// process that dies, not an exception the graph could have caught.
//
// Model calls are counted from `ledger.jsonl`, which the fake provider appends
// and fsyncs before it answers. The agent tree is never asked how much work it
// did, and the crash point is chosen by watching that ledger rather than by
// anything the driver reports about itself.
import { spawn } from "node:child_process";
import { join } from "node:path";
import { PROVIDER_URL } from "./config.ts";
import { entriesFor, reset } from "./ledger.ts";

const HERE = import.meta.dirname;
const TSX = join(HERE, "node_modules/.bin/tsx");
const ARM = process.env.BENCH_ARM ?? "Inconvo";
const REPEATS = Number(process.env.BENCH_REPEATS ?? 5);
const CONTROL = PROVIDER_URL.replace(/\/v1$/, "");

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

type Trial = {
  /** Model calls on the first attempt, before any crash. */
  first: number;
  /** Model calls on the retry. Every one of these is work paid for twice. */
  retry: number;
  /** Distinct call sites that ran on both attempts. */
  repeated: number;
  answered: boolean;
};
type Row = { name: string; trials: Trial[] };

async function waitForProvider(): Promise<void> {
  for (let i = 0; i < 100; i++) {
    try {
      if ((await fetch(`${CONTROL}/control/health`)).ok) return;
    } catch {}
    await sleep(100);
  }
  throw new Error("provider did not come up");
}

async function setAttempt(run: string, attempt: number): Promise<void> {
  await fetch(`${CONTROL}/control/attempt`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ run, attempt }),
  });
}

/** Spawns one answer. If killAfter is set, the process is SIGKILLed as soon as
 *  the ledger shows that many model calls, which is a crash partway through an
 *  answer with real work already paid for and nothing durable beneath it. */
function runDriver(run: string, thread: string, killAfter = 0): Promise<boolean> {
  return new Promise((resolve) => {
    const child = spawn(TSX, [join(HERE, "driver.ts")], {
      cwd: HERE,
      env: { ...process.env, BENCH_RUN: run, BENCH_THREAD: thread },
      stdio: ["ignore", "pipe", "pipe"],
    });

    let out = "";
    let err = "";
    child.stdout.on("data", (d) => (out += d));
    child.stderr.on("data", (d) => (err += d));

    let watcher: NodeJS.Timeout | undefined;
    if (killAfter > 0) {
      watcher = setInterval(() => {
        if (entriesFor(run).length >= killAfter) child.kill("SIGKILL");
      }, 25);
    }

    const guard = setTimeout(() => child.kill("SIGKILL"), 120_000);
    child.on("close", (code) => {
      clearTimeout(guard);
      if (watcher) clearInterval(watcher);
      // A deliberate kill is the scenario, not a failure. Anything else
      // non-zero is a harness or integration fault and should be visible.
      if (code !== 0 && code !== null && killAfter === 0) {
        console.error(`  driver exited ${code}: ${(err || out).slice(0, 300)}`);
      }
      resolve(out.includes('"ok":true'));
    });
  });
}

async function trial(killAfter: number): Promise<Trial> {
  const run = `run-${Math.random().toString(16).slice(2, 8)}`;
  const thread = `thread-${run}`;

  await setAttempt(run, 1);
  const firstOk = await runDriver(run, thread, killAfter);
  const first = entriesFor(run).filter((e) => e.attempt === 1);

  if (killAfter === 0) {
    return { first: first.length, retry: 0, repeated: 0, answered: firstOk };
  }

  // The retry. A new process, same conversation thread, so the outermost
  // checkpointer is available to it exactly as it would be in production.
  await setAttempt(run, 2);
  const retryOk = await runDriver(run, thread, 0);
  const retry = entriesFor(run).filter((e) => e.attempt === 2);

  const firstSites = new Set(first.map((e) => e.site));
  const repeated = new Set(
    retry.map((e) => e.site).filter((s) => firstSites.has(s)),
  ).size;

  return { first: first.length, retry: retry.length, repeated, answered: retryOk };
}

async function scenario(name: string, killAfter: number): Promise<Row> {
  const trials: Trial[] = [];
  for (let i = 0; i < REPEATS; i++) trials.push(await trial(killAfter));
  return { name, trials };
}

const avg = (xs: number[]) => (xs.reduce((a, b) => a + b, 0) / xs.length).toFixed(1);

async function main(): Promise<number> {
  reset();
  const provider = spawn(TSX, [join(HERE, "provider.ts")], {
    cwd: HERE,
    stdio: ["ignore", "ignore", "inherit"],
    env: process.env,
  });
  await waitForProvider();

  console.log();
  console.log(`  ${ARM} -- real inconvoAgent graph and real sub-agents, inconvo@fa63f29.`);
  console.log("  One question. compile({ checkpointer }) appears once, on the outermost");
  console.log("  graph; every sub-agent is compiled without one and invoked inside a node.");
  console.log();
  console.log(
    `  ${"scenario".padEnd(38)}${"calls".padStart(7)}${"re-run".padStart(8)}   what it costs`,
  );
  console.log("  " + "-".repeat(86));

  const control = await scenario("control, one answer, no crash", 0);
  const calls = Number(avg(control.trials.map((t) => t.first)));

  // Crash once the answer is far enough in that real work has been paid for.
  const killAfter = Math.max(2, Math.floor(calls / 2));
  const crashed = await scenario("crash mid-answer, then retry", killAfter);

  const rows: Row[] = [control, crashed];

  for (const r of rows) {
    const n = r.trials.length;
    const first = avg(r.trials.map((t) => t.first));
    const rerun = avg(r.trials.map((t) => t.retry));
    const repeated = avg(r.trials.map((t) => t.repeated));
    const unanswered = r.trials.filter((t) => !t.answered).length;

    const notes: string[] = [];
    if (Number(rerun) > 0) {
      notes.push(`${rerun} model calls paid for twice`);
      notes.push(`${repeated} of the same call sites re-run`);
    }
    if (unanswered > 0) notes.push(`no answer returned, ${unanswered} of ${n} runs`);
    const what = notes.length ? notes.join("; ") : "correct";

    console.log(
      `  ${r.name.padEnd(38)}${first.padStart(7)}${rerun.padStart(8)}   ${what}`,
    );
  }

  provider.kill();
  console.log();

  // Same gate as the other benchmarks: if the control row is not clean, the
  // harness is measuring itself and no other row means anything.
  const controlOk = control.trials.every((t) => t.answered && t.first > 0);
  if (!controlOk) {
    console.log("  Control must answer the question and make at least one model call.");
    console.log("  It did not, so every other row is a harness fault, not a finding.");
    return 1;
  }

  console.log(`  ${REPEATS} runs per row. Correct is answering once and paying once.`);
  console.log();
  console.log("  calls    model calls on the first attempt");
  console.log("  re-run   model calls on the retry, every one of them already paid for");
  console.log();
  return 0;
}

main().then((code) => process.exit(code));
