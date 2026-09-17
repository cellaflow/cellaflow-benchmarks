# Skyvern doesn't buy it twice. It deadlocks the task.

Run against **real Skyvern** — real `execute_step`, real Playwright, real
Postgres, real retry path. The only substitution is the LLM that decides which
element to click, replaced with a canned planner so the run is deterministic and
costs nothing. Skyvern v1.0.53, commit `d23ceb4`.

```
run 1   crash injected the instant ActionHandler.handle_action returns
        exit 137, order placed once

run 2   Skyvern's own retry: same task, new step at retry_index=1
        StepUnableToExecuteError:
          Reasons: ['another_step_is_running_for_task:tsk_...']

orders placed: 1
task status:   running, forever
```

Postgres afterwards:

```
stp_...100 | running | retry_index 0     <- the process that died
stp_...762 | created | retry_index 1     <- the retry, blocked
task       | running
```

## What actually happens

`agent_functions.py:1542`:

```python
steps = await app.DATABASE.tasks.get_task_steps(task_id=task.task_id, ...)
has_no_running_steps = not any(step.status == StepStatus.running for step in steps)
```

That is a claim check with **no lease, no heartbeat, and no expiry**. A step is
marked `running` at `agent.py:3819` — the only place in the codebase that sets
it — and nothing ever reaps a stale one. We looked.

So the guard works exactly as designed while the process is alive, and when the
process dies it becomes a tombstone. The order was placed once, which is
correct. No further step on that task can ever run, which is not.

## This corrects our own earlier model

The harness one directory up predicted **2 orders** on this crash, by
reproducing the `handle_action` → persist ordering without the running-step
guard. That prediction is **wrong**, and running the real thing is what caught
it. Skyvern is better on duplicates than the model implied, and has a failure
the model did not contain.

The corrected reading: Skyvern's behaviour here is the **`claim` arm** of the
[crash benchmark](https://github.com/cellaflow/cellaflow-sdks/tree/main/examples/crash_benchmark)
— charges once, then deadlocks the key. Not the `no-guard` arm.

That is the single row where a lease separates: `claim` deadlocks the ticket for
the whole fleet permanently, a lease expires and the work completes.

## What Skyvern already built, two days before this run

`alembic/versions/2026_09_15_...eee46e1b1dbf_add_durable_workflow_terminal_side_effect_progress.py`
adds `interim_side_effects_progress` and `final_side_effects_progress` to
`workflow_run_attempts`, tracking `webhook_delivery_attempted` across attempts so
a retried workflow run does not re-send its completion webhook.

So the premise is not in dispute. Skyvern made one side effect durable across
retries on 2026-09-15. That work is scoped to Skyvern's own webhook, at
workflow-run-attempt granularity. The browser actions inside a run, and the
running-step guard that blocks their retry, are a different layer.

## Run it

```bash
docker run -d --name sk-pg -e POSTGRES_USER=skyvern -e POSTGRES_PASSWORD=skyvern \
  -e POSTGRES_DB=skyvern -p 5440:5432 postgres:14-alpine
git clone https://github.com/Skyvern-AI/skyvern && cd skyvern && git checkout d23ceb4
alembic upgrade head          # DATABASE_STRING=postgresql+psycopg://skyvern:skyvern@localhost:5440/skyvern
pip install "./skyvern[server]" && playwright install chromium
python checkout_server.py 8899 &
python harness.py
```

No API key. No LLM spend. Deterministic.

### The one thing we change about Skyvern's configuration

`ALLOWED_HOSTS=["127.0.0.1"]`, because Skyvern blocks navigation to loopback as
an SSRF guard (`webeye/navigation.py:61`) and the checkout page under test is
local. It goes through the allow-list Skyvern already provides
(`utils/url_validators.py:_is_allowed_host`) and changes nothing about action
execution, persistence or retry.

Everything else is Skyvern's, including the guard that produced the result.

## What this does not establish

**Whether a human clears the stuck step in practice.** In a deployment the task
may be visible as stuck and cleared by an operator or a support process, which
turns a deadlock into a delay. We cannot see that from outside.

**Whether the click is always the last thing before the crash.** The crash is
injected at the boundary; a real crash lands wherever it lands. Earlier in the
batch and the same guard applies with fewer actions completed.

**That this is common.** It is one deterministic reproduction of one failure.

## The question

Has a Skyvern run ever been left stuck this way in your deployment — a task
`running` with a step that never finished? The failure is quiet, so we cannot
tell low incidence from low visibility. We would like to hear either answer.
