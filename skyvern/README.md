# A Skyvern run that dies mid-checkout buys the thing twice

Skyvern validates an `idempotency_key` — carefully, with bounds checks and its own
test file. It dedups **the HTTP request that creates a workflow run**:

```python
# skyvern/forge/sdk/routes/agent_protocol.py:998
digest = calculate_sha256(f"create_workflow\0{current_org.organization_id}\0{idempotency_key}")
```

That is the right thing to do and it is done well. It says nothing about the
browser actions *inside* the run, which is where the money moves.

```
  arm               control  crash: during  crash: after record    5 race
  -----------------------------------------------------------------------
  skyvern-shaped          1              2                    2         5
  cellaflow               1              2                    1         1
```

Orders placed against one order id. `1` is correct in every cell.

## The ordering, from the source

`skyvern/forge/agent.py`, commit `d23ceb4`, v1.0.53:

```python
4288:  results = await ActionHandler.handle_action(...)   # the click happens
4299:  detailed_agent_step_output.actions_and_results[action_idx] = (action, results)
       # in-memory only; the step's output is persisted after the action loop
```

No `create_action` precedes `handle_action` on this path. There is one at
`4152`, but that is the internal-refresh branch and it `break`s before reaching
the dispatch above.

So between a click landing on a page and the step being written, there is a
window — and it is not a narrow one. It spans every remaining action in the
batch, the inter-action waits, and post-action artifact recording. A process
that dies anywhere inside it leaves the external world moved with nothing
durable saying so, and Skyvern's own retry (`step.retry_index`,
`max_retries_per_step`, `agent.py:8548-8626`) runs the step again.

## Reading the two crash columns

**`crash: during` is a tie, and we are not claiming otherwise.** Dying between
the action and *any* durable record of it is unsurvivable for everything here,
ours included. The click is not a database write, so no lock, lease or
transaction can bracket it. Making the record transactional makes it strictly
worse: the write rolls back and the purchase stands. Both arms buy twice.

**`crash: after record` is the column that separates**, and the difference is
*when the operation becomes durable*, not whether the crash is survivable. A
leased tool records the operation as part of performing it, so its window is one
RPC. Skyvern's window is the rest of the action loop. Same failure, different
exposure.

**`5 race`** is a redelivered webhook or a double-submitted task — the case the
create-time idempotency key covers at the API boundary and not inside the run.

## What this is not

**It does not run Skyvern.** Skyvern's unit of work is an LLM deciding what to
click: nondeterministic, needs API keys and a browser, not reproducible by a
reader. What runs here is the *ordering*, against a fake checkout endpoint.

That makes this a statement about the ordering rather than a measurement of
Skyvern end to end. The ordering is cited above with line numbers so it can be
checked in ten seconds. **If it is wrong, the result is worthless, and we would
genuinely like to be told.**

It is also not a performance benchmark. The leased arm is slower — it adds a
round trip per guarded action. The axis here is correctness only.

## Run it

```bash
docker compose up -d          # only the cellaflow arm needs this
pip install -r requirements.txt
python harness.py --writers 5
```

The control column is load-bearing: one worker, no crash, no race, and it must
read `1` for every arm. If it does not, every other number is a harness bug and
the run says so and exits non-zero.

The ledger adjudicates. `place_order` appends to `ledger.jsonl` and fsyncs
*before* returning, so an arm cannot avoid a count by dying before it reports
one. No arm reports its own success.

## What would change these numbers

- A `create_action` call before `handle_action`, giving a retry something to
  observe. That closes `crash: after record` without needing anything external.
- An action-level idempotency key derived from the action's own content, so a
  replayed step recognises a click it has already made.
- Either would move the `skyvern-shaped` row and we would update this file.

## The question we cannot answer from outside

Has this actually bitten anyone? The failure is quiet — a duplicate order, found
later by a customer rather than by an alert — so we cannot tell low incidence
from low visibility. If you run Skyvern against anything that charges a card or
submits a form, we would like to hear which it is.

---

Built with [CellaFlow](https://github.com/cellaflow/cellaflow-sdks) — an
idempotency layer for AI agent tool calls. The
[crash benchmark](https://github.com/cellaflow/cellaflow-sdks/tree/main/examples/crash_benchmark)
runs the same method against four guards and six failures, including the rows
where CellaFlow ties no guard at all.
