# A crashed Skyvern run strands the task, and the way out of that is what orders twice

Measured against **real Skyvern** — real `execute_step`, real Playwright, real
Postgres, real retry chain. The LLM that picks which element to click is replaced
by a canned planner. Nothing else is substituted. Skyvern v1.0.53, commit
`d23ceb4`.

```
  scenario                              orders  correct   outcome
  --------------------------------------------------------------------------
  control, no crash                          1        1   correct
  crash before the click, then retry         0        0   task stranded
  crash after the click, then retry          1        1   task stranded
  action fails mid-batch                     1        1   step completes, no retry
  stranded task, operator reruns             2        1   fresh run re-orders
  two processes, one task                    2        1   both processes act
```

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
docker run -d --name sk-pg -e POSTGRES_USER=skyvern -e POSTGRES_PASSWORD=skyvern \
  -e POSTGRES_DB=skyvern -p 5440:5432 postgres:14-alpine

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
