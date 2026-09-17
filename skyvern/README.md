# A crashed Skyvern run strands the task, and the way out of that is what orders twice

Measured against **real Skyvern** — real `execute_step`, real Playwright, real
Postgres, real retry chain. The LLM that picks which element to click is replaced
by a canned planner. Nothing else is substituted. Skyvern v1.0.53, commit
`d23ceb4`.

```
  scenario                              orders  correct   outcome
  --------------------------------------------------------------------------
  control, no crash                          1        1   correct
  crash before the click, then retry         0        1   task stranded
  crash after the click, then retry          1        1   task stranded
  action fails mid-batch                     1        1   step completes, no retry
  stranded task, operator reruns             2        1   fresh run re-orders
  two processes, one task                    2        1   both processes act
```

Orders placed against one order id. `correct` is what the customer should end up
with: exactly one order, and the work finished. Row two reads `0` because the
stranded task means the order is never placed at all -- the failure shows in the
count. Row three reads `1` because the order was placed correctly; its failure
is that the task can never finish, which the outcome column carries instead.

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


Three findings.

## 1. Any crash during a step strands the task

`agent_functions.py:1542` gates every step on whether another is already running:

```python
steps = await app.DATABASE.tasks.get_task_steps(task_id=task.task_id, ...)
has_no_running_steps = not any(step.status == StepStatus.running for step in steps)
```

A claim with **no lease, no heartbeat and no expiry**. `StepStatus.running` is
written in exactly one place (`agent.py:3819`), and nothing reaps a stale one.

A process that dies mid-step leaves its step `running` permanently. The retry is
refused with `StepUnableToExecuteError: ['another_step_is_running_for_task:...']`.
Postgres afterwards:

```
stp_...100 | running | retry_index 0     the process that died
stp_...762 | created | retry_index 1     the retry, blocked
task       | running
```

**This is not about side effects.** Crashing *before* the click places no order
and strands the task identically. Any process death during a step is enough.

## 2. The way out of a stranded task is what duplicates the order

The finding that matters, and it is the two rows read together.

A stranded task cannot be retried — by Skyvern or by anyone. The only route to
getting the customer their order is a new run, which is what an operator or the
customer will do.

A new task starts with an **empty action history**. It navigates to the checkout
page, sees the button, clicks it. **Two orders.**

What prevents a repeat *within* a task is the action history Skyvern puts in the
planner's prompt. That history does not cross tasks, so the remediation for
finding 1 removes the thing that was preventing finding 2.

## 3. The running-step check is read-then-act, and it races

Two processes on one unclaimed task both read "no running steps", both proceed,
both click. **Two orders.** There is no lock between the check at `:1542` and the
`running` write at `agent.py:3819`.

This models a redelivered queue message, or two workers claiming one task.

---

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

## What Skyvern does guard, measured

Tested and found sound, which is worth stating precisely because the rows above
are the exceptions:

- **Repeat actions within a task.** The planner's prompt carries prior actions
  and their results — `{"action_type": "click", "status": "completed",
  "element_id": "AAAC"}` with `{"result": {"success": true}}` — alongside the
  changed URL. A model reading that will not re-click. The defence holds for
  every scenario inside a single task.
- **Completed tasks.** Cannot be re-run: `invalid_task_status:completed`.
- **A failed action mid-batch.** Does not force a step retry. The step completes
  and execution continues (`stop_execution_on_failure: False`), so earlier
  successful actions in the batch are not repeated.
- **Run creation.** The `create_workflow` idempotency key dedups the request that
  starts a run (`routes/agent_protocol.py:998`).

## What Skyvern shipped on 2026-09-15

`alembic/versions/...eee46e1b1dbf_add_durable_workflow_terminal_side_effect_progress.py`
adds `interim_side_effects_progress` and `final_side_effects_progress` to
`workflow_run_attempts`, tracking `webhook_delivery_attempted` across attempts so
a retried run does not re-send its completion webhook.

One side effect, made durable across retries, at workflow-run-attempt
granularity. The three findings above sit a layer in from it.

## Run it

```bash
docker compose up -d --wait

git clone https://github.com/Skyvern-AI/skyvern && (cd skyvern && git checkout d23ceb4)
DATABASE_STRING=postgresql+psycopg://skyvern:skyvern@localhost:5440/skyvern \
  alembic -c skyvern/alembic.ini upgrade head

python -m venv venv && ./venv/bin/pip install "./skyvern[server]"
./venv/bin/playwright install chromium

./venv/bin/python checkout_server.py 8899 &
./venv/bin/python harness.py
```

No API key. No LLM spend. Deterministic.

The control row is load-bearing: one run, no crash, one order. If it reads
anything else, every other row is a harness fault and the run says so.

Orders are counted from `ledger.jsonl`, which the checkout server appends and
fsyncs inside the POST handler, before responding. A run cannot avoid a count by
dying after the order.

**The one Skyvern setting changed** is `ALLOWED_HOSTS=["127.0.0.1"]`, because
Skyvern blocks loopback navigation as an SSRF guard (`webeye/navigation.py:61`)
and the checkout page is local. It uses the allow-list Skyvern already provides
and changes nothing about action execution, persistence or retry.

## Scope

- The crash is injected at a chosen boundary. A real crash lands wherever it
  lands; finding 1 holds for any of them, since it needs only a `running` step.
- Finding 3 was produced by running two processes against one task directly.
  Whether Skyvern's scheduler can deliver a task twice is a question about their
  infrastructure, not their agent loop.
- Whether stranded tasks get noticed and cleared in a real deployment is not
  visible from outside, and it decides whether finding 2 is routine or rare.

---

Built alongside [CellaFlow](https://github.com/cellaflow/cellaflow-sdks), which
leases tool calls so the record of an operation outlives the process performing
it. The [crash benchmark](https://github.com/cellaflow/cellaflow-sdks/tree/main/examples/crash_benchmark)
measures the same class of failure across four guards, including the rows where a
lease buys nothing.
