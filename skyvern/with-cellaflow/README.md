# The same Skyvern, with an execution lease and a lease per operation

Identical harness, identical scenarios, identical canned planner, identical
Skyvern v1.0.53 at `d23ceb4` as [the audit one level up](../). The only difference is
[`integration.py`](integration.py) and two call sites — nothing inside Skyvern.

```
  scenario                            Skyvern  + CellaFlow  correct
  ---------------------------------------------------------------------
  control, no crash                         1          1         1
  crash before the click, then retry        0          1         1   fixed
  crash after the click, then retry         1          2         1   traded
  action fails mid-batch                    1          1         1
  stranded task, operator reruns            2          2         1   unchanged
  two processes, one task                   2          1         1   fixed

  three operations, crash between 2 and 3
    reserve stock                           1          1         1
    charge card                             1          1         1
    send confirmation                       0          1         1   fixed
```

## What that table says

A row is correct when the customer ends up with exactly one order **and** the
task finishes. A right order count on a permanently dead task does not count,
because nothing downstream ever learns the work succeeded.

The control row is a harness check — nothing fails in it and both columns pass —
so the honest comparison excludes it. **Of the six rows where something actually
goes wrong, Skyvern handles one correctly. With the leases, four.**

**Three rows move:**

- *crash before the click* — a silently dropped request becomes a completed order
- *two processes, one task* — a double charge becomes a single one
- *three operations, crash between two and three* — a customer who paid and never
  heard anything gets their confirmation

**Two do not, and they fail for different reasons:**

- *crash after the click* — **not fixed, traded.** A permanently dead task
  becomes a second charge. Both are wrong; they are wrong in different
  directions, and only one leaves you something to reconcile.
- *operator reruns* — **unchanged.** The crash lands between the click and the
  commit, so nothing anywhere recorded it. No lease can bracket a side effect
  that is not a database write.

The one row Skyvern already gets right — a failed action mid-batch — stays
right.

## What actually changes for a customer

What the customer experiences in each case, running Skyvern on its own versus
Skyvern with an execution lease over the task and a lease on each irreversible
operation:

| what goes wrong | Skyvern alone | Skyvern + CellaFlow |
| :--- | :--- | :--- |
| crash part-way through a multi-step checkout | **charged, no confirmation ever sent, unrecoverable** | order completes |
| crash before the agent acts | request silently dropped, nobody notified | retry completes it |
| two workers pick up the same task | **charged twice** | charged once |
| crash immediately after the agent acts | one order, but the task is dead forever | order completes, **charged twice** |
| someone restarts a stuck run by hand | **charged twice** | **charged twice** |

Three situations stop costing anything. One trades a dead task for a duplicate.
One is unchanged.

## What the leases are

Skyvern's step guard (`agent_functions.py:1542`) asks whether any step on the
task is `running`. That answers *did someone start this*. It cannot answer *is
that someone still here*, which is the only question that matters once a process
has died — a status column has no way to express liveness.

A heartbeated lease can, and both halves are shipped SDK surfaces as of
**cellaflow 0.7.0**:

**`async_execution_lease`, keyed on the task.** Heartbeated while the worker
lives. Holding it means every `running` step on that task belongs to a holder
that stopped heartbeating — a tombstone, not a live worker. That is what makes
clearing it safe, and it is the question Skyvern's column cannot answer. If the
lease is lost mid-run the calling task is cancelled rather than continuing
unprotected.

**`durable_tools` + `@tool`, keyed on the business operation** — `charge:ORD-x`,
not the task, step or run. The idempotency cache is position-independent, so a
retry under a new step id derives the same key and takes a hit.

`clear_stale_running_steps` in `integration.py` is the only code this adds beyond
calling those two. It is short because the lease already answered the hard part.

## What changed, and what did not

**Crash before the click — the stranding is gone.** Skyvern places no order and
the task can never run again; the customer's request is silently dropped. With
the lease the retry finds the dead holder's step is a tombstone, clears it, and
completes the work.

**Two processes on one task — the race is closed.** Skyvern's check is
read-then-act and both workers click. The lease is the arbitration point, so the
second worker is refused before it starts.

**Three operations, crash between two and three — the row a lock cannot reach.**
Skyvern charges the card, holds the stock, and never sends the confirmation, with
no retry able to finish it. With the leases, `reserve` and `charge` return their
**stored results** and only `confirm` executes. The run log shows each mechanism
doing its part:

```
[driver] cleared 1 stale running step(s)
[driver] lease returned a prior result for reserve; not repeating it
[driver] lease returned a prior result for charge; not repeating it
```

**The row was run a third time to see which lease does what**, with the
per-operation leases switched off and the execution lease left on:

```
  operation            Skyvern   execution lease only   + operation leases   correct
  ---------------------------------------------------------------------------------
  reserve stock              1                      2                    1         1
  charge card                1                      2                    1         1
  send confirmation          0                      1                    1         1
```

Reading the `execution lease only` column, which is the one worth understanding:

1. The first worker reserves stock, charges the card, and dies.
2. Its lease stops being renewed, so the retry can prove the holder is gone and
   clear the step it left `running`. The log shows
   `cleared 1 stale running step(s)`. **The task un-sticks**, where Skyvern on
   its own leaves it dead.
3. The retry then runs the batch again — and nothing anywhere records that
   reserve and charge already completed. Zero cache hits. It performs all three.
4. Stock reserved twice, **card charged twice**, confirmation sent once.

So arbitrating ownership on its own does not fix this row. It converts a
stranded task into a double charge — a different failure, not a solved one.

**What that column stands for.** Any mechanism that answers *is anyone else
working on this* and releases when the holder dies: a `pg_advisory_lock`, a
Redis lock, a TTL'd leases table in the Postgres Skyvern already runs. None of
them answer *what did the work return*, which is the question that decides
whether the card is charged again.

It is a proxy for that class, not for any one of them. An advisory lock in
particular differs elsewhere — its liveness is connection liveness rather than a
heartbeat, it pins a database connection for the whole operation, and it carries
no fencing token. On *this* row those differences do not change the outcome,
because the only properties in play are ownership with reclaim, and no record of
results.

The `+ operation leases` column adds the second property. Note the counts: this
row holds **one** execution lease, keyed on the task, and **three** operation
leases — `reserve:ORD-x`, `charge:ORD-x`, `confirm:ORD-x`, one per irreversible
call. The retry asks whether `charge` for this order already completed, gets the
stored answer back, and skips it.

**Crash after the click — a trade, not a win.** Skyvern places one order and
strands the task forever; with the lease the task finishes and there are two
orders. *Charge once and deadlock* against *charge twice and complete*. Neither
column is correct. They fail differently, and only one leaves something you can
reconcile.

**Operator reruns — unchanged, and not fixable here.** The crash lands between
the click and the lease commit, so nothing anywhere recorded the order and the
next run has nothing to match. This is the `crash: during` window: the click is
not a database write, so no lease can bracket it.

What does change is the window's size. Skyvern's runs from the click to the end
of the action loop — every remaining action, the inter-action waits, artifact
recording. The leased one is a single RPC. A test aimed squarely at the window
hits both; a crash arriving at a random moment does not.

Worth noting this row also becomes *less reachable*: it exists because stranding
forces someone to start a fresh run, and stranding is now fixed. It remains a
risk if a user retries by hand.

## What this does not claim

Two of seven rows are still wrong with the integration in place. It closes the
stranding, the race and the half-done sequence, trades the fourth, and leaves the
unsurvivable window alone. Anyone reading this as *solved* has not read the
table.

## Run it

```bash
docker compose up -d --wait          # Postgres for Skyvern, CellaFlow for the leases

git clone https://github.com/Skyvern-AI/skyvern && (cd skyvern && git checkout d23ceb4)
DATABASE_STRING=postgresql+psycopg://skyvern:skyvern@localhost:5440/skyvern \
  alembic -c skyvern/alembic.ini upgrade head

python -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/playwright install chromium

./venv/bin/python checkout_server.py 8899 &
PYTHONPATH=. ./venv/bin/python harness.py
```

No API key. No LLM spend. Deterministic.

The control row is load-bearing: one run, no crash, one order. Anything else and
every other row is a harness fault.

Orders are counted from `ledger.jsonl`, which the checkout server appends and
fsyncs inside the POST handler before responding.
