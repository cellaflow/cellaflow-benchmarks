// The same scenarios as the audit one level up, with a leasing proxy between
// Inconvo and the model endpoint.
//
// The driver is not copied. `../driver.ts` is spawned exactly as the audit
// spawns it, with BENCH_PROVIDER_URL pointing at the proxy instead of straight
// at the fake provider, so the graph, the sub-agents and the fixture are the
// same code running the same way.
import { spawn } from "node:child_process";
import { join } from "node:path";
import { entriesFor, reset } from "../ledger.ts";

const HERE = import.meta.dirname;
const UP = join(HERE, "..");
const TSX = join(UP, "node_modules/.bin/tsx");
const REPEATS = Number(process.env.BENCH_REPEATS ?? 5);

const PROVIDER_PORT = 8317;
const PROXY_PORT = 8319;
const CONTROL = `http://127.0.0.1:${PROVIDER_PORT}`;
const PROXY = `http://127.0.0.1:${PROXY_PORT}`;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

type Trial = { first: number; retry: number; repeated: number; answered: boolean };
type Row = { name: string; trials: Trial[] };

async function waitFor(url: string, what: string): Promise<void> {
  for (let i = 0; i < 200; i++) {
    try {
      if ((await fetch(url)).ok) return;
    } catch {}
    await sleep(100);
  }
  throw new Error(`${what} did not come up`);
}

async function setAttempt(run: string, attempt: number, parkAfter = 0): Promise<void> {
  await fetch(`${CONTROL}/control/attempt`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ run, attempt, parkAfter }),
  });
}

async function setThread(thread: string): Promise<void> {
  await fetch(`${PROXY}/control/thread`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ thread }),
  });
}

function runDriver(run: string, thread: string, killAfter = 0): Promise<boolean> {
  return new Promise((resolve) => {
    const child = spawn(TSX, [join(UP, "driver.ts")], {
      cwd: UP,
      env: {
        ...process.env,
        BENCH_RUN: run,
        BENCH_THREAD: thread,
        // The only difference from the audit arm.
        BENCH_PROVIDER_URL: `${PROXY}/v1`,
      },
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

    const guard = setTimeout(() => child.kill("SIGKILL"), 180_000);
    child.on("close", (code) => {
      clearTimeout(guard);
      if (watcher) clearInterval(watcher);
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

  await setThread(thread);
  await setAttempt(run, 1, killAfter);
  const firstOk = await runDriver(run, thread, killAfter);
  const first = entriesFor(run).filter((e) => e.attempt === 1);

  if (killAfter === 0) {
    return { first: first.length, retry: 0, repeated: 0, answered: firstOk };
  }

  // Same conversation thread, so the proxy derives the same keys and the engine
  // still holds what attempt 1 paid for.
  await setThread(thread);
  await setAttempt(run, 2, 0);
  const retryOk = await runDriver(run, thread, 0);
  const retry = entriesFor(run).filter((e) => e.attempt === 2);

  const firstSites = new Set(first.map((e) => e.site));
  const repeated = new Set(retry.map((e) => e.site).filter((s) => firstSites.has(s))).size;

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
  const provider = spawn(TSX, [join(UP, "provider.ts")], {
    cwd: UP,
    stdio: ["ignore", "ignore", "inherit"],
    env: process.env,
  });
  await waitFor(`${CONTROL}/control/health`, "provider");

  const proxy = spawn(TSX, [join(HERE, "proxy.ts")], {
    cwd: HERE,
    stdio: ["ignore", "ignore", "inherit"],
    env: { ...process.env, BENCH_PROXY_PORT: String(PROXY_PORT), BENCH_UPSTREAM: CONTROL },
  });
  await waitFor(`${PROXY}/control/health`, "leasing proxy");

  const stop = () => {
    provider.kill();
    proxy.kill();
  };

  console.log();
  console.log("  Inconvo + CellaFlow -- same graph, same sub-agents, inconvo@fa63f29.");
  console.log("  Every model call is forwarded through a leasing proxy, so each one");
  console.log("  commits its result and a retry is handed that result back.");
  console.log();
  console.log(
    `  ${"scenario".padEnd(32)}${"calls".padStart(7)}${"retry".padStart(7)}` +
      `${"total".padStart(7)}${"wasted".padStart(8)}   what it costs`,
  );
  console.log("  " + "-".repeat(92));

  const control = await scenario("control, one answer, no crash", 0);
  const calls = Number(avg(control.trials.map((t) => t.first)));
  const killAfter = Math.max(2, Math.floor(calls / 2));
  const crashed = await scenario("crash mid-answer, then retry", killAfter);

  // Same yardstick as the audit arm: the clean answer. Anything above it was
  // paid for twice.
  const baseline = Number(avg(control.trials.map((t) => t.first)));

  for (const r of [control, crashed]) {
    const n = r.trials.length;
    const first = avg(r.trials.map((t) => t.first));
    const retry = avg(r.trials.map((t) => t.retry));
    const total = (Number(first) + Number(retry)).toFixed(1);
    const wasted = Math.max(0, Number(total) - baseline).toFixed(1);
    const repeated = avg(r.trials.map((t) => t.repeated));
    const unanswered = r.trials.filter((t) => !t.answered).length;

    const notes: string[] = [];
    if (r.name.startsWith("crash")) {
      notes.push(
        Number(wasted) > 0
          ? `${wasted} model calls paid for and thrown away`
          : "nothing paid for twice",
      );
      if (Number(repeated) > 0) notes.push(`${repeated} call sites re-run`);
    }
    if (unanswered > 0) notes.push(`no answer returned, ${unanswered} of ${n} runs`);
    const what = notes.length ? notes.join("; ") : "correct";

    console.log(
      `  ${r.name.padEnd(32)}${first.padStart(7)}${retry.padStart(7)}` +
        `${total.padStart(7)}${wasted.padStart(8)}   ${what}`,
    );
  }

  stop();
  console.log();

  const controlOk = control.trials.every((t) => t.answered && t.first > 0);
  if (!controlOk) {
    console.log("  Control must answer the question and make at least one model call.");
    console.log("  It did not, so every other row is a harness fault, not a finding.");
    return 1;
  }

  console.log(`  ${REPEATS} runs per row. Correct is answering once and paying once.`);
  console.log();
  console.log("  calls    model calls that reached the provider before the crash");
  console.log("  retry    model calls that reached the provider on the retry");
  console.log("  total    what one answer cost end to end");
  console.log("  wasted   total minus the clean answer: work paid for and thrown away");
  console.log();
  return 0;
}

main().then((code) => process.exit(code));
