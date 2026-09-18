# What happens to a Skyvern task when a process dies

An audit of **real Skyvern** — real `execute_step`, real Playwright, real
Postgres, real retry chain. The only substitution is the LLM that picks which
element to click, replaced by a canned planner so the run is deterministic and
costs nothing. Skyvern v1.0.53, commit `d23ceb4`.

```
  scenario                              orders  correct   outcome
  --------------------------------------------------------------------------
  control, no crash                          1        1   correct
  crash before the click, then retry         0        1   task stranded
  crash after the click, then retry          1        1   task stranded
  action fails mid-batch                     1        1   step completes, no retry
  stranded task, operator reruns             2        1   fresh run re-orders
  two processes, one task                    2        1   both processes act

  three operations, crash between 2 and 3
    reserve stock                            1        1
    charge card                              1        1
    send confirmation                        0        1   sequence stranded
```

**A row is correct when the customer ends up with exactly one order and the task
finishes.** Both halves matter: a right order count on a task that can never
complete is not a correct outcome, because nothing downstream ever learns the
work succeeded.

By that measure Skyvern gets **two of seven** — the control, where nothing goes
wrong, and the mid-batch failure, which it handles properly. Setting the control
aside as a harness check rather than a test of anything: **of the six rows where
something actually fails, one ends correctly.**

Here is what the other five cost, worst first:

| what happens | what it costs |
| :--- | :--- |
| a crash mid-sequence | **the card is charged, the confirmation never sent, and no retry can ever finish it** |
| a crash before any action | the customer's request is silently dropped — no order, no error, no alert |
| an operator restarts a stuck run | **the customer is charged twice** |
| two workers get the same task | **the customer is charged twice** |
| a crash after an action | one correct order on a task that can never complete, so nothing downstream knows it succeeded |

None of these surface as an error to anyone. That is the through-line: every
failure here is silent.

## What each row is

The agent's job in every scenario is the same: visit a checkout page and press
**Place order** once. What differs is what goes wrong while it does that.

**control, no crash.** One run, nothing interrupts it. Places one order. This row
exists to prove the harness works — if it reads anything but `1`, every other
number is a fault in the test and not a finding about Skyvern.

**crash before the click, then retry.** The worker decides to click and the
process is killed *before the click is sent*. Nothing irreversible happened. A
second run then retries the same task, which is what a worker pool or a
supervisor does. **Correct is one order** — the retry should finish the job.

**crash after the click, then retry.** The process is killed the instant the
click lands. The order *is* placed. A second run retries the same task.
**Correct is one order and a finished task.**

**action fails mid-batch.** Skyvern executes actions in batches. Here the order
succeeds and a later action in the same batch fails. **Correct is one order** —
the failure must not repeat the successful actions.

**stranded task, operator reruns.** After a crash leaves a task unable to
continue, someone starts a *fresh* run for the same order. This is what you do
when a run is visibly stuck, and it is the only option when the task itself
cannot be retried. **Correct is one order.**

**two processes, one task.** Two workers pick up the same task at the same
moment — a redelivered queue message, or a duplicate dispatch. **Correct is one
order.**

**three operations, crash between 2 and 3.** The checkout becomes three
separately-irreversible operations in one action batch: reserve stock, charge
card, send confirmation. The process dies after the second. **Correct is each
operation exactly once.**

## 1. Any crash during a step strands the task, permanently

`agent_functions.py:1542` gates every step on whether another is already running:

```python
steps = await app.DATABASE.tasks.get_task_steps(task_id=task.task_id, ...)
has_no_running_steps = not any(step.status == StepStatus.running for step in steps)
```

A claim with **no lease, no heartbeat and no expiry**. `StepStatus.running` is
written in exactly one place (`agent.py:3819`) and nothing reaps a stale one.

The retry is refused with
`StepUnableToExecuteError: ['another_step_is_running_for_task:...']`. In Postgres:

```
stp_…100 | running | retry_index 0     the process that died
stp_…762 | created | retry_index 1     the retry, blocked
task     | running
```

**Impact.** The work stops and nothing says so. Not an error, not a failed
status — the task reads `running` forever, so any dashboard or alert keyed on
failure sees nothing wrong. Whoever was waiting on that order waits indefinitely.

**This is not about side effects.** Crashing *before* the click places no order
and strands the task identically. Any process death during a step is enough.

## 2. The way out of a stranded task is what duplicates the order

A stranded task cannot be retried — by Skyvern or by anyone. The only route to
getting the customer their order is a new run, which is what an operator or the
customer will do.

A new task starts with an **empty action history**. It navigates to the checkout
page, sees the button, clicks it. **Two orders.**

**Impact.** The remediation for finding 1 *is* the duplicate. Someone clears a
stuck run and charges the customer a second time, and the two events look
unrelated in any log — a stuck task here, a new successful run there.

## 3. The running-step check is read-then-act, and it races

Two processes on one unclaimed task both read "no running steps", both proceed,
both click. **Two orders.** There is no lock between the check at `:1542` and the
`running` write at `agent.py:3819`.

**Impact.** A redelivered queue message or a duplicate dispatch — ordinary in any
worker pool — charges the customer twice, with both runs reporting success.

## 4. A crash mid-sequence leaves irreversible work half-done

The row where a lock is not enough. Three separately-irreversible operations in
one action batch; the process dies between the second and the third.

```
  reserve stock        1
  charge card          1
  send confirmation    0     and no retry can ever send it
```

**Impact.** The worst outcome in this document. The customer has paid, the stock
is committed, and the confirmation will never be sent. The order exists in the
payment processor and nowhere in any completion path. Nobody is notified, and the
stranding means no automatic recovery is possible.

Note this is not a duplicate problem. Skyvern does not repeat the charge here —
it simply never finishes, which is a different failure and in most businesses a
worse one.

## The record survives. The planner is not shown it.

The cheapest thing on this page to fix, and it changes more than one row.

Skyvern persists an action **inside** `handle_action`, before that function
returns (`webeye/actions/handler.py:4925`):

```python
action.finished_at = naive_utc_now()
persisted_action = await app.DATABASE.workflow_params.create_action(...)
action.action_id = persisted_action.action_id
return results
```

So a crash immediately after a click still leaves a durable row. Confirmed in
Postgres after exactly that crash:

```
actions:  click | completed | AAAC        <- survived
steps:    stp_…500 | running | output=∅   <- never written
```

But the history the planner is given comes from step outputs, not from those
rows (`services/action_service.py:29`):

```python
for window_step in window_steps:
    if window_step.output and window_step.output.actions_and_results:
```

`step.output` is written when a step *ends*. A crash mid-step never writes it. So
the retry's first prompt reads:

```
Action history from previous steps:
[]
```

**Skyvern knows the click happened and does not tell the planner.** The model is
asked what to do next with no record of what it already did, on a page that still
shows the button — and a model with no other information clicks it.

**Impact, and it is the constructive one.** Sourcing that history from the
`actions` rows, which already exist and already carry `status` and `element_id`,
closes the memory rows with no new infrastructure and no new dependency. It would
not fix findings 1, 3 or 4, which are about ownership rather than memory.

## What Skyvern guards correctly, measured

Tested and found sound. Worth stating precisely, because the rows above are the
exceptions rather than the rule:

- **Repeat actions within a task.** The planner's prompt carries prior actions
  and their results alongside the changed URL. A model reading that will not
  re-click. The defence holds for every scenario inside a single task.
- **Completed tasks.** Cannot be re-run: `invalid_task_status:completed`.
- **A failed action mid-batch.** Does not force a step retry; the step completes
  and execution continues (`stop_execution_on_failure: False`), so earlier
  successful actions are not repeated.
- **Run creation.** The `create_workflow` idempotency key dedups the request that
  starts a run (`routes/agent_protocol.py:998`).

## What Skyvern shipped on 2026-09-15

`alembic/versions/...eee46e1b1dbf_add_durable_workflow_terminal_side_effect_progress.py`
adds `interim_side_effects_progress` and `final_side_effects_progress` to
`workflow_run_attempts`, tracking `webhook_delivery_attempted` across attempts so
a retried run does not re-send its completion webhook.

One side effect, made durable across retries, at workflow-run-attempt
granularity. The findings above sit a layer in from it — same reasoning, applied
to the actions inside a run rather than the webhook after it.

## Run it

```bash
docker compose up -d --wait

git clone https://github.com/Skyvern-AI/skyvern && (cd skyvern && git checkout d23ceb4)
DATABASE_STRING=postgresql+psycopg://skyvern:skyvern@localhost:5440/skyvern \
  alembic -c skyvern/alembic.ini upgrade head

python -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/playwright install chromium

./venv/bin/python checkout_server.py 8899 &
./venv/bin/python harness.py
```

No API key. No LLM spend. Deterministic.

The control row is load-bearing: one run, no crash, one order. If it reads
anything else, every other row is a harness fault and the run says so.

Orders are counted from `ledger.jsonl`, which the checkout server appends and
fsyncs inside the POST handler before responding. A run cannot avoid a count by
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

[`../skyvern-with-cellaflow/`](../skyvern-with-cellaflow/) runs the identical
harness with two leases added, and reports which of these rows move.
