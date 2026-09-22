// Runs every scenario against real Corsair and prints the table.
//
// Each scenario spawns `driver.ts` as its own OS process, so "two instances"
// means two processes sharing one SQLite credential store, which is what a
// customer running two replicas of their API actually has.
//
// Refreshes are counted from `ledger.jsonl`, which the provider appends and
// fsyncs before responding. Nothing self-reports.
import { spawn } from "node:child_process";
import { existsSync, unlinkSync } from "node:fs";
import { join } from "node:path";
import { PROVIDER_URL } from "./config.js";
import { entriesFor, reset } from "./ledger.js";
import { openStore, seed } from "./keystore.js";

const HERE = import.meta.dirname;
const TSX = join(HERE, "node_modules/.bin/tsx");
const ARM = process.env.BENCH_ARM ?? "Corsair + CellaFlow";

type Trial = { calls: number; rejected: number; alive: boolean; torn: boolean };
type Row = { name: string; trials: Trial[] };

const REPEATS = Number(process.env.BENCH_REPEATS ?? 5);

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function waitForProvider(): Promise<void> {
  for (let i = 0; i < 100; i++) {
    try {
      const r = await fetch(`${PROVIDER_URL}/health`);
      if (r.ok) return;
    } catch {}
    await sleep(100);
  }
  throw new Error("provider did not come up");
}

function runDriver(run: string, db: string, crash = ""): Promise<void> {
  return new Promise((resolve) => {
    const child = spawn(TSX, [join(HERE, "driver.ts")], {
      cwd: HERE,
      env: { ...process.env, BENCH_RUN: run, BENCH_DB: db, BENCH_CALLERS: "4", BENCH_CRASH: crash },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let err = "";
    child.stderr.on("data", (d) => (err += d));
    const kill = setTimeout(() => child.kill("SIGKILL"), 60_000);
    child.on("close", (code) => {
      clearTimeout(kill);
      // 137 is the deliberate crash; anything else non-zero is a real failure.
      if (code !== 0 && code !== 137) console.error(`  driver exited ${code}: ${err.slice(0, 300)}`);
      resolve();
    });
  });
}

async function trial(processes: number, grace: boolean, crash = ""): Promise<Trial> {
  const run = `run-${Math.random().toString(16).slice(2, 8)}`;
  const db = join(HERE, `${run}.sqlite`);

  await fetch(`${PROVIDER_URL}/seed`, {
    method: "POST",
    body: new URLSearchParams({ run, token: "r0", grace: grace ? "1" : "0" }).toString(),
  });
  await seed(db, "r0");

  if (crash !== "") {
    // One process exchanges the token and dies before writing it back. Then a
    // retry arrives, which is what a worker pool or the next request does.
    await runDriver(run, db, crash);
    await runDriver(run, db);
  } else {
    // Genuinely concurrent processes. spawnSync would serialise them, and a
    // second process that starts after the first has finished simply finds a
    // fresh token and never refreshes -- which measures nothing.
    await Promise.all(Array.from({ length: processes }, () => runDriver(run, db)));
  }

  const store = await openStore(db);
  const stored = await store.km.get_refresh_token();
  const storedAccess = await store.km.get_access_token();
  await store.close();
  if (existsSync(db)) unlinkSync(db);

  const state = (await (await fetch(`${PROVIDER_URL}/valid?run=${run}`)).json()) as {
    valid: string | null;
  };
  const entries = entriesFor(run);
  // The provider always mints `aN` and `rN` together, so a stored pair whose
  // numbers disagree can only have come from two different refreshes merged on
  // top of each other. That is precisely the lost update Corsair's own comment
  // at key-manager.ts:189 warns about: "writers in other instances/processes
  // can still race".
  const n = (v: string | undefined) => (v ? v.slice(1) : "");
  const torn = Boolean(stored && storedAccess && n(stored) !== n(storedAccess));

  return {
    calls: entries.filter((e) => e.event === "refresh_accepted").length,
    rejected: entries.filter((e) => e.event === "refresh_rejected").length,
    alive: stored !== undefined && stored === state.valid,
    torn,
  };
}

// The racing rows are genuinely nondeterministic: which process persists last
// decides whether the surviving credential is the current one. One sample would
// be a coin flip reported as a finding, so every scenario is repeated.
async function scenario(name: string, processes: number, grace = false, crash = ""): Promise<Row> {
  const trials: Trial[] = [];
  for (let i = 0; i < REPEATS; i++) trials.push(await trial(processes, grace, crash));
  return { name, trials };
}

async function main(): Promise<number> {
  reset();
  const provider = spawn(TSX, [join(HERE, "provider.ts")], {
    cwd: HERE,
    stdio: ["ignore", "ignore", "inherit"],
    env: process.env,
  });
  await waitForProvider();

  console.log();
  console.log(`  ${ARM} -- real createAccountKeyManager and real singleFlight, corsair@4f268cdf.`);
  console.log("  One expired credential. Four concurrent callers inside each process.");
  console.log("  The provider rotates its refresh token and invalidates the previous one.");
  console.log();
  console.log(
    `  ${"scenario".padEnd(40)}${"exchanges".padStart(10)}   what happened`,
  );
  console.log("  " + "-".repeat(84));

  const rows: Row[] = [
    await scenario("control, one process", 1),
    await scenario("two processes, strict provider", 2),
    await scenario("two processes, grace-window provider", 2, true),
    await scenario("crash before anything records it", 1, false, "after_exchange"),
    await scenario("crash after the exchange is recorded", 1, false, "after_commit"),
  ];

  for (const r of rows) {
    const n = r.trials.length;
    const avg = (r.trials.reduce((a, t) => a + t.calls, 0) / n).toFixed(1);
    const failed = r.trials.filter((t) => t.rejected > 0).length;
    const broken = r.trials.filter((t) => !t.alive).length;
    const torn = r.trials.filter((t) => t.torn).length;

    // Say what happened in words. A column of fractions makes the reader hold
    // the denominator and its direction in their head, and every row here means
    // something different to a customer.
    const notes: string[] = [];
    if (broken > 0) notes.push(`INTEGRATION BROKEN, ${broken} of ${n} runs`);
    if (failed > 0) notes.push(`a user request failed, ${failed} of ${n} runs`);
    if (torn > 0) notes.push(`credential torn, ${torn} of ${n} runs`);
    if (Number(avg) > 1) notes.push(`token spent ${avg} times per expiry`);
    const what = notes.length ? notes.join("; ") : "correct";

    console.log(`  ${r.name.padEnd(40)}${avg.padStart(10)}   ${what}`);
  }


  // Does the failure get worse with more callers, or does it plateau? The
  // README claims the two-process rows are a floor, and that claim needs a
  // number rather than an assertion.
  console.log();
  console.log(`  ${"callers on one credential".padEnd(40)}${"exchanges".padStart(10)}   what happened`);
  console.log("  " + "-".repeat(84));

  for (const procs of [2, 4, 8]) {
    const r = await scenario(`${procs} replicas, strict provider`, procs);
    const n = r.trials.length;
    const avg = (r.trials.reduce((a, t) => a + t.calls, 0) / n).toFixed(1);
    const failed = r.trials.reduce((a, t) => a + t.rejected, 0) / n;
    const brokeN = r.trials.filter((t) => !t.alive).length;
    const note = brokeN > 0
      ? `INTEGRATION BROKEN, ${brokeN} of ${n} runs`
      : `${failed.toFixed(1)} failed requests per run`;
    console.log(`  ${r.name.padEnd(40)}${avg.padStart(10)}   ${note}`);
  }

  provider.kill();
  console.log();
  const control = rows[0];
  const controlOk = control.trials.every((t) => t.calls === 1 && t.alive && t.rejected === 0 && !t.torn);
  if (!controlOk) {
    console.log("  Control must spend exactly one refresh and leave the connection");
    console.log("  working. It did not, so every other row is a harness fault.");
    return 1;
  }
  console.log(`  ${REPEATS} runs per row. Correct is one exchange and nothing else.`);
  console.log();
  console.log("  INTEGRATION BROKEN     the stored credential no longer works, so the end");
  console.log("                         user has to reconnect the app");
  console.log("  a user request failed  a caller got invalid_grant back");
  console.log("  credential torn        the stored access and refresh tokens came from two");
  console.log("                         different exchanges, merged over each other");
  console.log("  token spent N times    one expiry cost more than one exchange");
  console.log();
  return 0;
}

main().then((code) => process.exit(code));
