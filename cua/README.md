# Cua repeats an irreversible operation after a crash, because nothing records that it happened

An audit of **real Cua** — real `ComputerAgent.run`, real agent loop, real
computer dispatch. Two things are replaced, both through Cua's own documented
extension points: the model, by a canned agent registered with
`@register_agent`, and the machine, by a `CustomComputerHandler` built from a
dict of functions. `cua-agent` 0.8.4.

No VM, no Lume, no display server, no API key, no LLM spend.

```
  scenario                                      reserve  charge  confirm
  --------------------------------------------------------------------
  control, no crash                                   1       1        1
  crash after an operation, then retry                2       2        1
```

The agent's job is three separately-irreversible operations: reserve stock,
charge the card, send the confirmation. The process is killed the instant the
card is charged. A supervisor retries the same order.

**The card is charged twice, and the stock is reserved twice.**

## What each row is

**control, no crash.** One run, nothing interrupts it. Each operation once. This
row exists to prove the harness works — anything else and every other number is
a fault in the test, not a finding about Cua.

**crash after an operation, then retry.** The process dies immediately after the
charge lands. A second run retries the same order, which is what a worker pool
or a supervisor does. **Correct is each operation exactly once.**

## Why it repeats

The retry is a fresh process, and nothing durable says which operations already
completed. Cua's trajectory capture is explicitly a debugging artifact —
`TrajectorySaverCallback`, and a CLI flag documented as *"Save trajectory for
debugging"* — not resumable state. There is no checkpoint and no resume.

So the new process starts with an empty history, looks at a screen that shows
the same three buttons, and does all three again.

The screen in this harness deliberately does not change when an operation
succeeds. That is the realistic case for anything confirming out of band — an
emailed receipt, an API call behind the UI — and it isolates the question: if
the pixels do not say what already happened, does anything else? Here, nothing
does.

## What Cua already guards, measured

Tested and found sound. Worth stating precisely, because it narrows the finding
to something specific:

- **Concurrent control of the machine.** The driver implements leases over
  desktop input, with expiry — `lease_expired` refusals and
  `Clock::now() >= expires` in the hyprland plugin, carrying a lease sequence
  and capabilities. There is a test file named
  `production_agent_conflict_proof.py`. Two agents cannot both drive the
  keyboard, and a dead holder's lease expires. **That half is done.**
- **Retrying a command whose outcome is unknown.** `cua_sandbox/transport/http.py`
  retries transient 5xx on `/cmd` and explicitly refuses to retry on transport
  timeouts, with the reasoning in the source: *"the command may already be
  running on the server, and most /cmd actions aren't idempotent."* That is the
  conservative and correct default.
- **Transient model failures.** `_predict_step_with_retry`
  (`cua_agent/agent.py:220`) retries prediction errors with backoff. It retries
  the model, not the action.

## So the gap is narrow, and it is specific

Cua can already tell you **nobody else is driving the machine**. It cannot tell
you **which operations already happened**.

The consequence shows up in its own retry policy. Because no record exists, a
timeout on `/cmd` is unresolvable — the command may or may not have run, so the
client gives up. That is safe, and it means the outcome is unknown and the work
is unrecoverable. A per-operation record turns *cannot retry, must fail* into
*retry safely, the record says what happened*.

## Run it

```bash
# cua-agent requires Python >=3.11,<3.14
uv venv --python 3.13 venv
uv pip install --python venv/bin/python cua-agent cellaflow

./venv/bin/python harness.py
```

Deterministic. The control row is load-bearing: one run, no crash, each
operation once. If it reads anything else, every other row is a harness fault
and the run says so.

Operations are counted from `ledger.jsonl`, which the fake machine appends and
fsyncs *before* returning, so a run that dies the instant after acting is still
counted as having acted.

## Scope

- The crash is injected at a chosen point. A real crash lands wherever it lands.
- The fake machine stands in for a desktop. Whether a duplicated action costs
  anything depends on what the desktop is driving: a click in a throwaway VM
  costs nothing, a payment page costs money. This harness models the latter.
- Nothing here is measured about VM provisioning, fleet lifecycle or billing,
  which is where Cua's own durability work lives.

---

[`with-cellaflow/`](with-cellaflow/) runs this same harness with a lease on each
irreversible operation, and reports which rows move.
