# Two replicas refreshing one OAuth token spend it twice, and a crash loses it

An audit of **real Corsair** at `4f268cdf`: real `createAccountKeyManager`, real
`singleFlight`, real read-merge-write of the encrypted credential blob. Nothing
about the credential path is reimplemented.

Two things are supplied by the harness: the OAuth provider, which is a local
fake so the run is deterministic and costs nothing, and a file-backed SQLite in
place of Corsair's `:memory:` test database, because an in-memory database
cannot be shared between processes.

| what goes wrong | token swaps | what it costs the customer |
| :--- | :---: | :--- |
| nothing, one process | 1.0 | correct |
| two replicas, strict provider | 1.0 | **a user request fails, every run** |
| two replicas, grace-window provider | **2.0** | the credential is spent twice for one expiry |
| crash before anything records it | 1.0 | **integration broken, every run** |
| crash after the exchange is recorded | 1.0 | **integration broken, every run** |

Five runs per row. **Correct is one exchange and nothing else.** "Integration
broken" means the stored credential no longer works, so the end user has to go
and reconnect the app.

One tenant has one expired access token. Four concurrent callers inside each
process ask for it.

## What is being counted

**token swaps** is how many times the provider was asked to exchange the refresh
token, counted from `ledger.jsonl`, which the provider appends and fsyncs before
it responds. Each swap issues a new refresh token and revokes the old one, so
one expired credential needs exactly one. Above 1.0 means the credential was
rotated more than once for a single expiry, and every caller still holding the
previous token now fails.

Three things can go wrong, and the harness names whichever occurred:

- **integration broken** is the severe one. The credential left in the database
  no longer works, so the end user has to reconnect the app.
- **a user request failed** means a caller got `invalid_grant` back.
- **credential torn** means the stored `access_token` and `refresh_token` came
  from two different exchanges. The provider always mints `aN` and `rN`
  together, so a mismatched pair can only be a lost update. Not observed in any
  run here, and reported only when it happens.

## What the rows show

**control, one process.** Corsair's `singleFlight` collapses the four concurrent
callers into a single exchange, every time. It works. This row exists to prove
the harness is measuring the process boundary and nothing else; if it reads
anything but clean, every other row is a harness fault.

**two processes, strict provider.** Both processes hold a valid-looking refresh
token, both try to exchange it, and the provider has already consumed it for
whoever arrived first. The second gets `invalid_grant`, **5 runs out of 5**. The
stored credential survives, because the loser has nothing to write. What the
customer sees is an API call that failed for no reason they can act on.

**two processes, grace-window provider.** Microsoft, Auth0 and Okta keep a
superseded refresh token briefly usable so a client retrying over a flaky
network is not punished. Under that provider both processes succeed and the
credential is exchanged **twice for one expiry**, every run.

**the two crash rows.** A process spends the refresh token at the provider and
dies before writing the result back. The old token is now dead at the provider
and the new one existed only in that process's memory. A retry reads the stale
credential, presents a token the provider has already consumed, and there is
nothing anywhere to repair it. **5 runs out of 5, the integration is broken and
the end user has to reconnect.**

Both crash rows are identical here because this arm has no durable record
between the exchange and the write. There is only one point at which to die.
That changes in the other arms, which is the whole point of measuring two.

## Why it happens, in their own words

`packages/corsair/core/auth/key-manager.ts:189`:

> Serialize config writes: each setter does a read-merge-write of the whole
> encrypted blob, so parallel setters (e.g. `Promise.all([set_a, set_b])`) read
> the same base and the last write silently drops the other's field.
> **Per-instance serialization only — writers in other instances/processes can
> still race; row-level merge or optimistic locking is the full fix.**

And `core/auth/oauth-access.ts` names the consequence exactly:

> two concurrent `/oauth/refresh` calls would spend the same rotating
> `refresh_token` and the second would fail

Corsair is a library you mount inside your own server
(`core/management/adapters/node.ts`). `singleFlight` is a `WeakMap` and the
write chain is a promise in a closure, so both live and die with one process.
Run two replicas of your API, which is the ordinary thing to do, and there is
nothing between them.

## The two arms

| | what it adds | concurrency rows | crash rows |
| :--- | :--- | :--- | :--- |
| this folder | nothing | **fail** | **fail** |
| [`with-cellaflow/`](with-cellaflow/) | a lease **and a durable record of the exchange** | pass | one passes, one fails |

The obvious alternative, a `pg_advisory_lock` around the refresh, was measured as
a third arm and is written up in
[`with-cellaflow/README.md`](with-cellaflow/README.md#what-an-advisory-lock-does-and-does-not-fix)
rather than kept as code. **It closes the concurrency rows completely and does
nothing for either crash row**, which is the finding worth having: the missing
piece is not mutual exclusion, it is a durable record of a side effect that
already happened.

Neither arm closes *"crash before anything records it"*. A refresh token spent at
the provider with no record anywhere is unrecoverable by construction. What the
record buys is shrinking the window, not removing it.

## Concurrency costs a request. It takes a crash to break the integration.

Both are worth stating precisely, because they point at different fixes.

**No torn credentials**, in 25 racing runs. The lost update their comment warns
about is real in principle and this harness did not produce one, because each
process writes its three fields in order immediately after its own exchange.

**No broken connections from concurrency alone.** Under a strict provider the
loser fails and writes nothing; under a grace-window provider both writes carry
a working token. Concurrency costs a failed request or a wasted exchange. It
takes a crash to break the integration.

## Two replicas is the conservative case. Agents are the likely one.

**This harness runs two replicas of one service, and that is a deliberate
choice, not a claim about where the contention comes from.** Two identical
processes are the only way to produce the race deterministically: same code,
same timing, reproducible on every run. Anything less predictable would make the
rows a coin flip.

The contention itself is not a property of replicas. It is a property of **one
credential with more than one caller**, and Corsair's own README names the shape
that produces most of them:

> Build anything, from an agent working across all your integrations to a
> multi-tenant dashboard for your users to connect to anything.

An agent working across a tenant's integrations holds exactly one credential per
integration. Two agents acting for that tenant, or one agent that a supervisor
fanned out, or an agent and a scheduled sync, all reach the same stored token
through the same key manager. From `singleFlight`'s point of view they are
indistinguishable from two replicas, because the `WeakMap` it dedupes against is
per-process either way.

**Three things make agents the harder case rather than an equivalent one**, and
none of them is measured here:

- **They arrive at unpredictable times and in unpredictable numbers.** Two
  replicas race in a window you can reason about. N agents racing is a
  distribution, and the tail is where a stale credential is read.
- **They do not know about each other.** Replicas of one service at least share a
  deployment and a config. Agents spawned by different triggers have no reason to
  coordinate and no channel to do it on.
- **They are correlated, not independent.** Anthropic's multi-agent research
  found agents converge rather than diverge: 18 of 30 independently chose an
  identical git branch name, and "when one agent makes a bad decision, it is
  likely that many agents will make that same bad decision". Correlated arrival
  is worse than random arrival for a shared credential, because it clusters the
  callers into the same instant instead of spreading them out.

**The numbers above are a floor, and the harness measures how much of one.**
Same credential, same strict provider, more callers:

| callers on one credential | token swaps | failed requests per run |
| ---: | :---: | :---: |
| 2 replicas | 1.0 | ~1 |
| 4 replicas | 1.0 | **~5** |
| 8 replicas | **~1.2** | **~14** |

Approximate because this is a race and the figures move between runs; the shape
is stable, the third decimal is not.

Failed requests grow **faster than the number of callers**: four times the
callers produces around five times the failures, eight times around fourteen.
And at eight, the swap count rises above 1.0, which means a strict provider that
revokes on reuse is being asked to rotate the credential more than once for a
single expiry. Each rotation invalidates the token every other caller is still
holding, so the failures compound rather than add.

With the lease, all three rows read **1.0 token swaps and 0 failed requests**.
The curve is flat because the contention is resolved before the provider is
called rather than by the provider rejecting the losers.

Agents are the reason a deployment would have more than two callers.

What would change the analysis, and is not measured here, is an agent doing
something a replica never does: proposing a *different* answer for the same work
at the same moment. Corsair's refresh path cannot produce that, because every
caller wants the identical thing, a fresh token, so there is nothing to disagree
about. That failure is measured separately in
[`../multi-agent/`](../multi-agent/).

## Run it

```bash
git clone https://github.com/corsairdev/corsair
cd corsair && git checkout 4f268cdf && cd ..

npm install
CORSAIR_SRC=$PWD/corsair/packages/corsair npx tsx harness.ts
```

No API key, no OAuth app, no network. About two minutes.

Corsair's dependencies are not installed: the harness imports
`core/auth/key-manager.ts` and `core/auth/single-flight.ts` directly, and those
two files need nothing but Node builtins. `sqlite-date-plugin.ts` is copied into
this folder rather than imported, because it is the one file in the path that
does import `kysely`. It is a serialisation adapter, not the code under test,
and the copy is byte-for-byte theirs.

## Scope

- The provider is a local fake. Real rotation behaviour varies by vendor, which
  is why both a strict and a grace-window provider are measured rather than one
  being asserted as typical.
- The crash is injected at a chosen boundary. A real crash lands wherever it
  lands; the two rows bracket the interesting range.
- Whether a given deployment has more than one caller per credential is a
  question about their users' architecture, not about Corsair's code. The
  finding is that nothing in the library notices if it does.
- The scaling table is measured. The claim that *agents* are what produce more
  than two callers is reasoning from Corsair's own stated use case, not a
  second measurement. Nothing here runs an LLM.
