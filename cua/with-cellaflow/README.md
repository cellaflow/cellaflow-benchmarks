# The same Cua, with a lease on each irreversible operation

Identical harness, identical canned agent, identical fake machine, identical
`cua-agent` 0.8.4 as [the audit one level up](../). The only difference is that
`click` routes each operation through a CellaFlow `@tool` keyed on the business
operation — `charge:ORD-x` — rather than on the run, the process or the agent
turn.

```
  scenario                                 Cua              + operation leases
                                    reserve charge confirm   reserve charge confirm
  ---------------------------------------------------------------------------------
  control, no crash                       1      1       1         1      1       1
  crash after an operation, then retry    2      2       1         1      1       1
  crash inside an operation, then retry   2      2       1         1      2       1
```

Correct is `1, 1, 1` in every row.

## What changes for a customer

| what goes wrong | Cua alone | Cua + operation leases |
| :--- | :--- | :--- |
| crash after the card is charged | **charged twice, stock reserved twice** | each done once |
| crash *while* the card is charging | **charged twice** | **charged twice** |

One row fixed, one unchanged. The difference between them is the whole of what a
lease can and cannot do.

## Why the first row moves

A retry is a fresh process with no memory, and Cua keeps no durable record of
which operations completed — trajectory capture is a debugging artifact. So the
new run repeats everything.

The operation lease is keyed on the work rather than the run, so the retry
derives the same key, finds a committed result, and does not perform the
operation again:

```
[cua] lease returned a prior result for reserve; not repeating it
[cua] lease returned a prior result for charge; not repeating it
```

Only `confirm`, which never ran, executes.

## Why the second row does not

The crash lands *inside* the charge — after the money moves, before the lease
commits. Nothing anywhere recorded it, so the retry has nothing to match
against and charges again.

**No lease closes this.** The charge is not a database write, so nothing can
bracket it, and making the record transactional is strictly worse: the write
rolls back and the money stays moved. The two rows differ only in whether the
crash lands before or after the record, and that is the honest boundary of what
this buys.

What changes is the size of the window, not its existence — from "the whole rest
of the run" down to the gap between the effect and one commit.

## Note on which lease this is

This arm uses **operation leases only** — `durable_tools` with a `@tool` per
irreversible call. It does **not** use an execution lease over the run.

That is a real difference from the Skyvern benchmark, where the execution lease
did most of the work. It is not needed here because Cua does not strand: its
driver already implements leases over desktop input with expiry, so a dead
holder's grip on the machine is already released, and a retry is already free to
proceed. Cua's gap is memory, not ownership, so only the half that supplies
memory is used.

## Run it

```bash
docker compose up -d --wait          # the engine, to hold the leases

uv venv --python 3.13 venv           # cua-agent requires >=3.11,<3.14
uv pip install --python venv/bin/python cua-agent cellaflow

BENCH_HAS_LEASES=1 ./venv/bin/python harness.py
```

No VM, no API key, no LLM spend.

Operations are counted from `ledger.jsonl`, which the fake machine appends and
fsyncs before returning.
