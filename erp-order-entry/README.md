# An agent that turns emailed purchase orders into ERP sales orders, crashed at the worst moment

A constructed benchmark rather than an audit. The agent here is small and
written for this repository, because the question is not whether one vendor's
agent framework has a gap: it is what happens to **an ERP write** when the
process doing it dies, or when two processes do it at once, or when the ERP
answers and the answer is lost on the way back.

The ERP is real. **ERPNext v16.53.0**, the official `frappe_docker` stack at
that pin, with MariaDB 11.8 behind it. The orders are real `Sales Order` documents
created and submitted through `/api/resource` with an API key, the same way an
integration would. The count at the end of every run is a query against
ERPNext. Nothing self-reports.

No API key for anything else, no model calls, no inference spend. The
extraction step, which a deployment would give to a model, is a parser.

Everything is pinned, arm C included: engine image
`ghcr.io/cellaflow/cellaflow@sha256:a6fb0484c49e...`,
SDK `cellaflow==0.8.0`. The exact digest is in `docker-compose.yml`.

**We build one of the four arms.** Cellaflow is ours, so this is not a neutral
comparison and should not be read as one. What is here instead is the raw
output in `results.json`, a harness small enough to read in a sitting, and a
table that reports the two rows our own arm loses. The most useful thing you
could send back is a number that does not reproduce.

## The result

Runs in which one customer PO number ended up carrying more than one Sales
Order. Five runs per cell, and every cell came out the same way in all five.

| | A. baseline | B. query-first guard | B2. ERPNext as it ships | C. Cellaflow leased write |
| :--- | :---: | :---: | :---: | :---: |
| control, no crash | 0/5 | 0/5 | 0/5 | 0/5 |
| crash after the record, before the job is marked done | **5/5** | 0/5 | 0/5 | 0/5 |
| two workers take the same inbox | **5/5** | **5/5** | 5/5 &dagger; | 0/5 |
| crash after the write, before any record of it | **5/5** | 0/5 | 0/5 | **5/5** |
| crash before the write | 0/5 | 0/5 | 0/5 | 0/5 |
| the ERP commits, the answer never arrives | **5/5** | 0/5 | 0/5 | **5/5** |

&dagger; Not a second order. Under concurrency ERPNext accepts both drafts and
then refuses the second submit, so every one of those runs ends with one
submitted order and one abandoned draft. Counting only submitted orders, that
cell is **0/5**, and it is the only cell in the table where the two counts
differ. The third table in the run output separates them.

Nothing went missing anywhere: no arm, in any scenario, failed to create an
order that should have existed.

**The headline is not the one this repository usually reports.** ERPNext, left
exactly as it ships, prevents a second submitted sales order in every row.
What it does not do is tell the agent that the work is already done. The
refused worker gets an error, is never handed the existing order number, and
never marks its job finished, so it will try again forever. What that costs is
the subject of "What the table says", further down.

## What is being modelled

Ten purchase orders arrive as plain-text email. For each one the agent reads
the mail, extracts the customer, the customer's PO number and one to three
line items, creates and submits a Sales Order against that PO number, and then
records the job as finished in its own SQLite table. That last step is what
almost every worker of this size does, and it is written with
`synchronous=FULL`, so it is not lost to a power cut.

The weakness is not that the agent's record is unreliable. It is that there
are **two** records of one event, in two systems, written one after the other,
and a process can stop between them.

## The arms

**A. baseline.** Create the order. Nothing guards it.

**B. query-first guard.** Before creating, ask ERPNext whether a Sales Order
already carries this customer PO number, and skip if one does. This is what a
competent team writes first, and it is in here so that the table is not a
comparison against a strawman.

**B2. ERPNext's own setting.** ERPNext ships a field in Selling Settings called
*Allow multiple Sales Orders against a customer's Purchase Order*, and it ships
it **unchecked**, meaning the ERP itself refuses the second order. Arms A, B
and C tick it, so that whatever stops a duplicate is the agent rather than the
ERP. Arm B2 is the only row that leaves ERPNext's refusal switched on, and it
is here because an honest benchmark against a destination that already guards
this write has to say so.

**C. Cellaflow leased write.** The same write, wrapped in a Cellaflow lease
keyed on the customer's purchase order number:

```python
@tool(idempotency_key=f"sales-order:{order.po_no}")
def create(po_no: str) -> dict:
    return erp.create_sales_order(order)
```

Nothing else changes. The key is derived from the work and from nothing else:
not the session, not the process, not the position in the run, not the
extracted line items. A retry in a fresh process derives the same string, and
so does a second worker that never shared memory with the first.

## How the files are arranged

There is no `with-cellaflow/` directory here, and that is deliberate. The other
benchmarks in this repository keep the leased arm in its own tree because the
subject is somebody else's codebase and the arm has to be a separate copy of
it. The agent here is ours and it is small, so a second copy would be four
files that differ in one function, and the question worth answering, which is
what exactly changes when you add a lease, would turn into a diff across two
directories instead of a few lines you can read in one place.

So all four arms are in one tree, selected at run time by `BENCH_ARM`:

| file | what it is |
| :--- | :--- |
| `fixtures/` | the ten purchase order emails, as plain text |
| `extract.py` | the deterministic stand-in for the model: sender to customer, a regex for the PO number, three quantity shapes |
| `agent.py` | the agent, and **the only file that differs between the columns of the table**. The arms are three write functions and a dict that maps the four column names onto them, because B2 is the baseline agent with ERPNext's own setting left on |
| `erp.py` | the ERPNext REST client, and the single place every injected fault lives, so the agent code is identical in all four arms |
| `jobstate.py` | the agent's own SQLite job table, at `synchronous=FULL` |
| `driver.py` | one worker process. The harness spawns it and kills it where the scenario says so |
| `harness.py` | the matrix: scenarios times arms times runs, the control gate, the reset between runs, the three output tables |
| `seed.py` | one-time ERPNext setup, run by `setup.sh`: company, customers, items, price list, API key |
| `docker-compose.yml` | the `pwd.yml` stack at v16.53.0, plus one engine service |
| `results.json` | the raw per-run counts behind every number in this README |

### Every line of Cellaflow in this benchmark

Five lines in `agent.py` and one service in `docker-compose.yml`. Nothing else
imports it: not the harness that scores the runs, not the ERP client that
counts the orders, not the extractor. An arm cannot be helped by anything
outside its own write.

The write itself, in `write_leased`, this time with the crash point in it:

```python
from cellaflow import tool

@tool(idempotency_key=f"sales-order:{order.po_no}")
def create(po_no: str) -> dict:
    result = erp.create_sales_order(order)
    _die("in-write", po_no)       # the crash point, inside the leased call
    return result
```

and the block it runs in, in `worker`, which is the only other place the arm is
named:

```python
if arm == "cellaflow":
    from cellaflow import durable_tools
    with durable_tools(session, target=ENGINE):
        ...
```

`ENGINE` defaults to `localhost:50051` and the compose file publishes the
engine there. The other three arms call `write_baseline` or `write_guarded`
through the same loop, so the loop, the extraction, the job table, the crash
points and the ERP client are shared and a difference in the table is a
difference in how the write is guarded.

## What each row is

**control, no crash.** One pass, nothing interrupts it, each email becomes one
order. This row is a gate: if it reads anything else, every other number in
the table is a fault in the harness rather than a finding about the arms, and
the run says so and stops.

**crash after the record, before the job is marked done.** The process dies
after the arm's own durable record exists and before the job table is updated.
Arms A and B keep no such record, so for them this is the same instant as the
crash-after-the-write row below, and the result is the same. Arm C has one, and
this is the row where having it shows.

**two workers take the same inbox.** Two processes, started together, each with
its own job table, both working the same ten emails. Two replicas polling one
mailbox is the ordinary way this arises, and it is modelled as two separate
job tables because that is what two pods have. Nothing local to one worker can
see the other.

**crash after the write, before any record of it.** The process dies in the
instant between ERPNext committing the order and anything at all noting that
it did. For arms A and B that instant is before the SQLite row. For arm C it is
also before the lease commits, because the crash is injected **inside** the
leased function. This is the window nothing closes, and putting the crash
anywhere more flattering would be the easiest way to make arm C look better
than it is.

**crash before the write.** A control in the other direction. Nothing has
happened yet, so a retry should simply do the work once, in every arm.

**the ERP commits, the answer never arrives.** ERPNext creates and submits the
order, and the client gets a read timeout instead of the response. The worker
cannot tell a write that landed from one that did not, so it fails the job and
the supervisor starts the work again. This is the only scenario where the first
process exits through its error handlers rather than being killed, and for arm C
that difference turns out to matter.

## What the table says

**The baseline duplicates in four rows out of six, every run.** None of this is
intermittent, and under concurrency it is not partial either: all ten orders
are written twice, in all five runs. An order written to an ERP by a process
that then dies, or by two processes at once, or by one that never heard the
answer, is written twice.

**ERPNext already guards this write, and the guard holds.** Arm B2 is the ERP
with nothing added, and it ends every row with one submitted order. Under
concurrency both workers get their draft in, because the check and the insert
are not one operation, and then the second submit is refused: `validate_po`
runs again at submit time and by then the first order is committed. So the
duplicate never becomes a commitment. It becomes a stray draft: eight of the
ten orders in four runs and nine in the fifth, with ten refusals every run.

**What that guard costs is the answer, not the order.** Across the four rows
where it fired, the refused worker got a validation error and nothing else: no
order number, no indication that the work it was sent to do is finished. It
does not mark the job done, so a supervisor will hand it back the same email
indefinitely. In the concurrency row that happened fifty times over five runs.
Correct orders, stuck jobs, and a person to reconcile the difference.

**The query-first guard closes four rows and hands back the order.** Arm B asks
the ERP for the PO number before writing, finds the order the dead process
left, and returns it: `reused 5/5` in all three crash and timeout rows. The
job completes. Its one failure is the concurrency row, and it is structural
rather than sloppy. The check and the write are two requests, so both workers
can pass the check before either writes. It catches most of them: three or four
of the ten orders slip through, every run.

**The lease closes the concurrency row and the one the other guards share.**
Arm C is the only arm that ends the two-worker row with one submitted order and
no stray draft, and the second worker is handed the first one's order number
rather than an error. In the crash-after-the-record row it does the same. Both
come from the same property: the key is the customer's purchase order number,
so a fresh process and a process that never shared memory with the first derive
the same string and reach the same committed result.

**The lease loses two rows, and they are the two where nothing was committed.**

In the crash-after-the-write row the process dies inside the leased function,
after ERPNext committed and before the lease did. The retry finds the lease
held by a process that no longer exists, waits out its expiry, and then does
the work again. It duplicates five runs out of five, the same as the baseline,
and it is slower about it, because the engine will not hand the position to
anyone else until the dead holder's lease times out.

In the lost-response row the leased call raises. The SDK treats an exception as
the signal that the side effect did not happen, calls `release_lease` with
reason `TOOL_ERROR` and re-raises, so the next attempt is let straight through.
A read timeout is precisely the case where that inference is wrong. Five runs
out of five, a second submitted order.

**No arm wins the table.** B and C between them cover every row, and neither
does alone. Against this particular destination the honest advice is to leave
ERPNext's own setting alone, because it is free and it holds, and then add
whichever of B or C answers the failure you actually have: a guard if your
destination can be asked what it already did, a lease if two workers can reach
it at once.

## The limits, stated plainly

**A crash between the side effect and the first durable record is not closed by
a lease, and is not closed by anything else either.** The order is in ERPNext
and the record of it is not yet anywhere. A retry has nothing to match against.
Making the record transactional with the write is not available: they are two
systems, and a transaction that rolled back would leave the order in the ERP
regardless. What a lease changes is the size of the window, from the whole rest
of the run down to one in-flight call. It does not reach zero, and the row
above says so.

**Where the destination accepts an idempotency key, use it.** That is the
stronger fix for that call, because it moves the decision inside the system
that performs the side effect and removes the gap entirely. ERPNext's REST API
does not offer one for `Sales Order`. Its PO-number check is the nearest thing
it has, which is why arm B2 is in the table.

**Where the destination is queryable by the business key, a guard gets most of
the way.** Arm B is not a strawman and the table does not treat it as one. It
reads the ERP before writing, and against this ERP that works, because `po_no`
is a field the REST API will filter on. It carries no index in the Sales Order
doctype, so the question costs a table scan, and ERPNext's own check in
`validate_po` pays the same price. Arm B's one structural failure is the
concurrency row: the check and the write are two requests, so two workers can
both pass the check before either writes. Its other limitation does not show up in this table
at all, which is that most calls an agent makes are not queryable this way. A
payment, an email, a webhook and a physical action have no equivalent of "has
this already been done" to ask.

**A leased call that raises releases its lease.** An exception is how the SDK is
told that the side effect did not happen, so it lets the next caller through. A
lost response is exactly the case where that inference is wrong, which is why
arm C does not close the last row. The workaround is not to let a timeout out
of the leased function, but that means resolving the ambiguity inside it, which
is arm B's query by another name.

## Run it

Needs Docker and about 8GB free for the ERPNext stack.

```bash
./setup.sh      # containers, site, ERPNext's default records, company, items, API key
./run.sh        # six scenarios, four arms, five runs each
```

One cell on its own:

```bash
BENCH_SCENARIOS=two-workers BENCH_ARMS=cellaflow ./run.sh
```

`BENCH_RUNS=1` shortens everything. `ERP_PORT`, `CELLAFLOW_PORT` and
`CELLAFLOW_HEALTH_PORT` move the published ports if something already holds
8080 or 50051.

Every run is real subprocesses, killed with `os._exit`, which skips `finally`
blocks, buffered writes and destructors, the same as a pod eviction. The ERP is
reset between runs and every run gives the ten emails fresh purchase order
numbers, so no run can inherit a previous one's state.

Raw per-run counts, including the docstatus of every order, land in
`results.json`.

## Scope

- The crash is injected at a chosen point. A real crash lands wherever it lands.
  The two crash rows are chosen to bracket the interesting boundary rather than
  to sample it.
- The read timeout is injected in the client, after ERPNext has committed,
  rather than by interfering with the network. It is the same position a client
  is left in, reached deterministically.
- Extraction is a parser, not a model. Every arm extracts identically and no
  scenario crashes during extraction, so the substitution is upstream of
  everything measured. An extractor that produced different line items on a
  retry would be a different and real failure, and it is measured in
  [`../multi-agent/`](../multi-agent/) rather than here.
- One engine, one ERP, one machine. Nothing here measures or claims anything
  about running more than one of either.
- `is_stock_item` is 0 on every item, so no warehouse, stock ledger or
  reservation is involved. A duplicate order here costs a duplicate commitment
  to deliver, not double-counted stock.
- The database and administrator passwords in `docker-compose.yml` are the
  upstream `pwd.yml` defaults, left as they are so the file stays diffable
  against it. Nothing in the stack is published beyond localhost. Do not point
  this compose file at anything that matters.
- Setting the stack up surfaced an unrelated race worth knowing about if you
  reproduce this: a fresh Frappe site generates its encryption key on first use,
  and with several containers running, two of them can generate different ones
  and the last write wins, which makes an API secret stored a moment earlier
  undecryptable. `setup.sh` seeds the keys after the site has settled.
