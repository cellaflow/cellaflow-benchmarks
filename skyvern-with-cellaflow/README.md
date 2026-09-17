# The same Skyvern, with two leases

Same harness, same scenarios, same canned planner, same Skyvern v1.0.53 at
`d23ceb4` as [the audit](../skyvern/). The only difference is
[`integration.py`](integration.py) — about eighty lines, none of them inside
Skyvern.

```
  scenario                            Skyvern  + CellaFlow  correct
  ---------------------------------------------------------------------
  control, no crash                         1          1         1
  crash before the click, then retry        0          1         1     fixed
  crash after the click, then retry         1          2         1     traded
  action fails mid-batch                    1          1         1
  stranded task, operator reruns            2          2         1     unchanged
  two processes, one task                   2          1         1     fixed
```

Orders placed against one order id.

## What each row is

The agent's job in every scenario is the same: visit a checkout page and press
**Place order** once. What differs is what goes wrong while it does that.

**control, no crash.** One run, nothing interrupts it. Places one order. This row
exists to prove the harness works — if it reads anything but `1`, every other
number is a fault in the test and not a finding about Skyvern.

**crash before the click, then retry.** The worker decides to click and the
process is killed *before the click is sent*. Nothing irreversible happened: no
order, no charge, nothing to deduplicate. A second run then retries the same
task, which is what a worker pool or a supervisor does. **Correct is one order** —
the retry should finish the job. Zero means the customer's request is silently
lost: no error reaches them, nothing alerts, and nobody is ever shipped anything.

**crash after the click, then retry.** The process is killed the instant the
click lands, before anything records that it did. The order *is* placed. A second
run retries the same task. **Correct is one order and a finished task.** Two
orders means the customer paid twice; a stuck task means the run never completes
and nobody knows the order went through.

**action fails mid-batch.** Skyvern executes actions in batches. Here the order
succeeds and a later action in the same batch fails. **Correct is one order** —
the failure must not cause the successful actions to be repeated.

**stranded task, operator reruns.** After a crash leaves a task unable to
continue, someone starts a *fresh* run for the same order. This is what you do
when a run is visibly stuck, and it is the only option when the task cannot be
retried. **Correct is one order** — the new run should recognise the work is
already done rather than redo it.

**two processes, one task.** Two workers pick up the same task at the same
moment. This models a redelivered queue message or a duplicate dispatch, which is
ordinary in any worker pool. **Correct is one order** — only one of them should
act.


## What the leases are

Skyvern's step guard (`agent_functions.py:1542`) asks whether any step on the
task is `running`. That answers *did someone start this*. It cannot answer *is
that someone still here*, which is the only question that matters once a process
has died — and a status column has no way to express it.

A heartbeated lease can, and that is the whole of what is added:

**An execution lease, keyed on the task.** Held for one worker's run and
heartbeated while it lives. Holding it means every `running` step on the task
belongs to a holder that stopped heartbeating — a tombstone, not a live worker.
That is what makes clearing it safe.

**An operation lease, keyed on the order.** Not on the task, the step or the run.
The idempotency cache is position-independent, so a fresh run started later
derives the same key.

Neither is a patch to Skyvern. The execution lease wraps the run; the operation
lease routes the checkout click through `@tool`.

**They use two different CellaFlow surfaces, and it is worth knowing which.** The
operation lease is `durable_tools` plus `@tool` — the shipped, ergonomic path.
The execution lease is the raw client (`check_idempotency_cache` / `renew_lease`
/ `release_lease`), deliberately: `@tool` commits a result on return, which is
memoization, and a second legitimate step on the same task would take a cache hit
and be skipped. Mutual exclusion wants acquire, heartbeat, release, with no
commit.

Which means the two fixed rows below are the **execution** lease's doing, not
`durable_tools`'. Wrapping the invocation in `durable_tools` alone would catch the
race late — after both workers have booted a browser and scraped — and would not
touch the stranding at all, because it has no visibility into Skyvern's `steps`
table. The row `durable_tools` is aimed at is *operator reruns*, and that is the
row that does not move.

## What changed, and what did not

**Crash before the click — the stranding is gone.** Skyvern leaves the task
permanently unretryable and the customer never gets their order: `0`. With the
lease, the retry finds the dead holder's step is a tombstone, clears it, and
completes the work: `1`. This is the row the integration exists for.

**Two processes on one task — the race is closed.** Skyvern's check is
read-then-act and both workers click: `2`. The lease is the arbitration point,
so the second worker never runs: `1`.

**Crash after the click — a trade, and it is the documented one.** Skyvern
places one order and strands the task forever. With the lease the task finishes
and there are two orders. That is *charge once and deadlock* against *charge
twice and complete*, which is exactly the trade the
[crash benchmark](https://github.com/cellaflow/cellaflow-sdks/tree/main/examples/crash_benchmark)
measures between a claim and a lease. Neither column is correct; they fail
differently, and one of them leaves you something to reconcile.

**Operator reruns — unchanged, and this one is not fixable here.** The crash
lands between the click and the lease commit, so nothing anywhere recorded that
the order was placed, and the next run has nothing to hit. This is the
`crash: during` window: the click is not a database write, so no lease can
bracket it. The crash benchmark says the same thing about our own arm.

What does change is the size of the window. Skyvern's runs from the click to the
end of the action loop — every remaining action in the batch, the inter-action
waits, artifact recording. The leased one is a single RPC. A test aimed squarely
at the window hits both; a crash arriving at a random moment does not.

## The row where a lock is not enough

The six rows above all reduce to *press one button exactly once*. That is mutual
exclusion, and mutual exclusion is cheap — a leases table in the Postgres
Skyvern already runs would do it. This row is on different ground.

A checkout becomes three separately-irreversible operations in one action batch.
The process dies between the second and the third.

```
  operation                             Skyvern   + CellaFlow   correct
  ---------------------------------------------------------------------
  reserve stock                               1             1         1
  charge card                                 1             1         1
  send confirmation                           0             1         1
                                        stranded      completes
```

**Skyvern does not duplicate here. It strands the sequence half-done.** The
card is charged, the stock is held, and the confirmation is never sent — and
because the dead worker's step is still `running`, no retry can ever finish it.
The customer has paid and will never hear anything.

**Both leases are load-bearing, and that is the point of the row.** The
execution lease proves the previous holder stopped heartbeating, which licenses
clearing its step and lets the retry proceed at all. The operation leases then
supply what a lock cannot: `reserve` and `charge` return their *stored results*
rather than being performed again.

A distributed lock gets you the first half. It would unstick the task and then
re-run the batch from the top, because a lock records who is holding it and not
what the work returned — `2, 2, 1`. The difference between that and `1, 1, 1` is
durable results, which is the one thing a leases table in Postgres does not have.

The batch shape is deliberate. `step.output` is written when a step *ends*, so
three separate steps would leave a populated action history and the retry would
correctly skip the first two on its own. Only a crash inside a single batch
loses it — which is also why the planner here reads Skyvern's action history and
skips anything already listed: on this crash it comes back empty, and the result
is about Skyvern rather than about a stub ignoring what it was given.

## What this does not claim

Two of six rows are still wrong with the integration in place. It closes the
stranding and the race, trades the third, and leaves the unsurvivable window
alone. Anyone selling this as *fixed* has not read the table.

## Run it

```bash
docker compose up -d --wait          # Postgres for Skyvern, CellaFlow for the leases

python -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/playwright install chromium
DATABASE_STRING=postgresql+psycopg://skyvern:skyvern@localhost:5440/skyvern \
  ./venv/bin/alembic upgrade head      # from a Skyvern checkout at d23ceb4

./venv/bin/python checkout_server.py 8899 &
PYTHONPATH=. ./venv/bin/python harness.py
```

No API key. No LLM spend. Deterministic.

The control row is load-bearing: one run, no crash, one order. Anything else and
every other row is a harness fault.

Orders are counted from `ledger.jsonl`, which the checkout server appends and
fsyncs inside the POST handler before responding.
