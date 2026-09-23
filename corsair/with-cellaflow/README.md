# The same Corsair, with a lease and a durable record of the exchange

Identical harness, identical fake provider, identical real
`createAccountKeyManager` and `singleFlight` as [the audit one level
up](../). Corsair's in-process dedupe is left exactly where it was.

One thing is added: the token exchange is wrapped in a CellaFlow leased tool,
keyed on the credential rather than on the process or the run.

```ts
const exchangeToken = tool(
  async (refreshToken: string) => { /* call the provider */ },
  { idempotencyKey: `corsair:refresh:${tenant}:${integration}`, toolName: "oauth_refresh" },
);
```

That does three things together, and the third is the one a lock cannot copy: it
admits one caller, it records what that caller produced, and it hands that record
to whoever asks next.

| what goes wrong | Corsair alone | with CellaFlow |
| :--- | :--- | :--- |
| nothing, one process | correct | correct |
| two replicas, strict provider | **a user request fails, every run** | correct |
| two replicas, grace-window provider | **credential spent twice per expiry** | spent once |
| crash before anything records it | **integration broken, every run** | **broken, every run** |
| crash after the exchange is recorded | **integration broken, every run** | **recovers, every run** |

Five runs per row. "Integration broken" means the stored credential no longer
works and the end user has to reconnect the app.

The harness races two replicas because that is reproducible. The same contention
arrives from two agents sharing one tenant's credential, which is Corsair's own
stated use case, and [the audit explains why agents are the harder
case](../#two-replicas-is-the-conservative-case-agents-are-the-likely-one). The
lease is keyed on the credential rather than on the caller, so it does not care
which of the two you have.

## The row that separates this from a lock

**`crash after the exchange is recorded`: broken every run becomes correct every run.**

A process takes the lease, spends the refresh token, records what it received,
and dies before writing it into Corsair's store. The retry asks for the lease,
is told the operation already completed, and is handed back what it produced:

```
[corsair] recovered a completed exchange; storing its tokens
```

It writes those tokens and the integration is intact. No second exchange, no
user reconnecting anything.

`pg_advisory_lock` cannot reach this row, and not for want of trying. A lock
answers *who may act*. It has nowhere to put *what they did*, so when the holder
dies the token it spent is simply gone. That distinction is the entire argument
for this arm, and it is why the advisory-lock arm is in the benchmark: without
it, a reader supplies the objection themselves and never sees it answered.

## The row that nothing fixes

**`crash before anything records it`: broken every run, in every arm including this
one.**

If the process dies between the provider rotating the token and anything
recording it, the token is spent and no record of it exists anywhere. No lease,
no lock and no journal closes that window, because the window is *before* the
first durable write.

What the record buys is shrinking the window from "the entire rest of the run"
to "the gap between the provider responding and one commit". It does not remove
it. A benchmark claiming otherwise would be lying, and this is the same limit
the [Cua benchmark](../../cua/) reports in its bottom row.

## What an advisory lock does, and does not, fix

The first thing a competent engineer reaches for on reading Corsair's comment is
`pg_advisory_lock` around the refresh. That was built and measured as a third arm
of this benchmark, then removed as code and kept here, because the result is more
useful than the directory was.

| what goes wrong | Corsair alone | + `pg_advisory_lock` | with CellaFlow |
| :--- | :--- | :--- | :--- |
| nothing, one process | correct | correct | correct |
| two replicas, strict provider | **request fails** | correct | correct |
| two replicas, grace-window provider | **spent twice** | correct | correct |
| crash before anything records it | **broken** | **broken** | **broken** |
| crash after the exchange is recorded | **broken** | **broken** | **recovers** |

**A lock closes every concurrency row and neither crash row.**

That is worth stating plainly, because it cuts against the obvious pitch. If the
problem were only "two processes at once", Corsair should add four lines of SQL
and be done. For those rows, they should.

What a lock cannot do is survive the holder dying. It answers *who may act* and
has nowhere to record *what they did*, so when the holder dies the completed work
is gone and the next process walks into the same wall a fraction sooner.

### Four things the pass/fail table hides

Those rows score correctness, so the lock and CellaFlow both read as "clean" on
the concurrency rows. They are not equivalent.

| | `pg_advisory_lock` | CellaFlow |
| :--- | :--- | :--- |
| holds a pool connection across the provider call | **yes**. Measured at 15.1s at pool 2, 6.2s at 5, 3.3s at 20 | no. Flat 3.0s |
| holder hangs without dying | **never releases.** Session-scoped, no TTL parameter exists, so it frees on connection *death* and not on a process wedged on a slow call | reclaimed at the lifetime ceiling |
| the app runs SQLite | **unavailable.** Advisory locks are Postgres-only, and Corsair supports both dialects | available |
| two replicas propose different arguments | serialises them, detects nothing | refused before the side effect |

The latency figures come from a separate harness measuring contention rather than
correctness, `docs/benchmarks/hand-rolled-idempotency.md` in the engine
repository. The second row is the one most likely to bite in production: a lock
released on connection death looks safe right up until a process is alive but
stuck, which is the ordinary shape of a slow model call.

### What this arm holds constant

Corsair's credential store stayed SQLite in every arm, including the lock one, so
the only thing differing was the coordination primitive. A real Corsair
deployment on Postgres would take the lock in the database that already holds the
credentials; the lock semantics are identical and that is what was being
compared.

## What it costs

**A hand-rolled table could also store the result.** Nothing here requires
CellaFlow specifically. A `refresh_results` table with the idempotency key as
primary key, written in the same transaction, would close the same row. What
that buys you is the job of writing lease expiry, fencing and result storage
yourself, and getting all three right. The honest claim is about **what is
needed**, not about what only one product can supply: a lock is insufficient,
and a durable record of the side effect is the missing piece.

**It adds a service.** Corsair's positioning is self-hosted with no
infrastructure beyond the database you already run. A gRPC daemon is a real
architectural ask against a benchmark whose worst measured outcome is a broken
OAuth connection after a crash.

**The SDK requires `@cellaflow/sdk` >= 0.7.1.** Earlier versions ship the
transport only, with no `tool` and no key derivation, so this arm cannot be
written against them.

## How the ordering works

The sequence matters more than any of the machinery:

```
acquire lease
  -> exchange the token at the provider     <- crash here and it is lost
  -> commit the result durably              <- crash here and it is recoverable
  -> write it into Corsair's store
```

The commit deliberately does **not** release the lease. A completed key owns
itself; releasing it would discard the record the next caller needs.

## Run it

```bash
git clone https://github.com/corsairdev/corsair
cd corsair && git checkout 4f268cdf && cd ..

docker compose up -d --wait
npm install
CORSAIR_SRC=$PWD/corsair/packages/corsair npx tsx harness.ts
```

The engine is the only service. Corsair drives a local fake provider, so there
is no VM, no OAuth app and no third-party account.
