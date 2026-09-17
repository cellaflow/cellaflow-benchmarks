# What happens to a Skyvern task when a process dies

Run against **real Skyvern** — real `execute_step`, real Playwright, real
Postgres, real retry chain. One substitution: the LLM that picks the element,
replaced by a canned planner, so the run is deterministic and costs nothing.
Skyvern v1.0.53, commit `d23ceb4`.

| # | scenario | orders placed | outcome |
| :-- | :--- | --: | :--- |
| 0 | control — one run, no crash | **1** | correct |
| 3 | crash **before** the click, retry same task | **0** | **task deadlocked** |
| — | crash **after** the click, retry same task | **1** | **task deadlocked** |
| 1a | action fails mid-batch, after the order | **1** | correct — step completes, no retry |
| 1b | second step, planner ignores action history | 2 | **retracted — see below** |
| 4 | after a deadlock, operator starts a **new task** | **2** | **duplicate order** |
| 2 | two processes race one task | **2** | **duplicate order** |

`1` is correct everywhere except row 3, where `0` is correct.

## The three findings

### 1. Any crash during a step deadlocks the task, permanently

`agent_functions.py:1542`:

```python
steps = await app.DATABASE.tasks.get_task_steps(task_id=task.task_id, ...)
has_no_running_steps = not any(step.status == StepStatus.running for step in steps)
```

A claim check with **no lease, no heartbeat, no expiry**. `StepStatus.running`
is set in exactly one place (`agent.py:3819`) and nothing reaps a stale one.

The retry then refuses with
`StepUnableToExecuteError: ['another_step_is_running_for_task:...']`. Postgres
afterwards: dead step `running`, retry step `created` at `retry_index=1`, task
`running`.

**This happens whether or not a side effect occurred** — row 3 crashes before
the click, places no order, and strands the task exactly the same way. It is not
a side-effect problem. It is any process death during a step.

### 2. The remediation for the deadlock is what produces the duplicate

This is the one that matters, and the two rows have to be read together.

The stuck task cannot be retried. The only way to get the customer their order
is to start a new run — which is what an operator or the customer will do. A new
task starts with an **empty action history**, navigates to the checkout page,
sees the button, and clicks it.

**Two orders.** The deadlock does not merely stall the work; it forces the
action that duplicates it.

### 3. The running-step check is read-then-act, and it races

Two processes on one unclaimed task both read "no running steps", both proceed,
both click. **Two orders.** There is no lock between the check at `:1542` and
the `running` write at `agent.py:3819`.

This models a redelivered queue message or two workers claiming the same task —
not an exotic case for a worker pool.

## What Skyvern gets right, and a test of ours that was unfair

**Row 1b is retracted.** It showed 2 orders from a planner that clicked whenever
it saw the button. That planner was ignoring information Skyvern supplied. The
second prompt contains:

```
Action history from previous steps:
[{"action": {"action_type": "click", "status": "completed", "element_id": "AAAC"},
  "result": {"success": true}}, ...]
```

plus a changed URL (`/place-order`). A real model would have seen both. Skyvern's
defence against repeating an action **within a task** is real and it works —
row 1a confirms it. The defence is a model reading history rather than a
mechanical guard, which is a fair thing to note and not a defect.

Also correct, and worth saying because we tested for it and did not find a
problem:

- A **completed** task cannot be re-run (`invalid_task_status:completed`).
- A **failed action mid-batch** does not force a step retry; the step completes
  and execution continues (`stop_execution_on_failure: False`).
- The `create_workflow` **idempotency key** dedups the run-creation request
  (`routes/agent_protocol.py:998`).

## What Skyvern shipped two days before this run

`alembic/versions/2026_09_15_...eee46e1b1dbf_add_durable_workflow_terminal_side_effect_progress.py`
adds `interim_side_effects_progress` and `final_side_effects_progress` to
`workflow_run_attempts`, tracking `webhook_delivery_attempted` across attempts so
a retried run does not re-send its completion webhook.

The premise is not in dispute. That work makes one side effect durable across
retries, at workflow-run-attempt granularity. Findings 1–3 are a layer in from
it.

## This corrects an earlier version of ours

The harness one directory up modelled the ordering without the running-step
guard and predicted duplicate orders on a simple crash. **That was wrong.**
Skyvern is better on that path than the model implied. Running the real thing is
what caught it, and it is why that file now carries a correction banner.

## Run it

```bash
docker run -d --name sk-pg -e POSTGRES_USER=skyvern -e POSTGRES_PASSWORD=skyvern \
  -e POSTGRES_DB=skyvern -p 5440:5432 postgres:14-alpine
git clone https://github.com/Skyvern-AI/skyvern && (cd skyvern && git checkout d23ceb4)
DATABASE_STRING=postgresql+psycopg://skyvern:skyvern@localhost:5440/skyvern \
  alembic -c skyvern/alembic.ini upgrade head
pip install "./skyvern[server]" && playwright install chromium
python checkout_server.py 8899 &
python harness.py
```

No API key. No LLM spend. Deterministic.

**The one Skyvern setting we change** is `ALLOWED_HOSTS=["127.0.0.1"]`, because
Skyvern blocks loopback navigation as an SSRF guard (`webeye/navigation.py:61`)
and the checkout page is local. It uses the allow-list Skyvern already provides
and changes nothing about action execution, persistence or retry.

## What this does not establish

- **Whether stuck tasks are noticed and cleared in practice.** An operator or a
  sweeper we could not find would turn the deadlock into a delay.
- **How often a crash lands mid-step** in a real deployment.
- **That the race is reachable through Skyvern's own scheduler.** We raced two
  processes directly; whether their queue can deliver one task twice is a
  question about their infrastructure, not their agent loop.

## The question

Has a run ever been left stuck this way in your deployment — a task `running`
with a step that never finished — and what happens next when it is? That last
part is the one we cannot see from outside, and it decides whether finding 2 is
theoretical or routine.
