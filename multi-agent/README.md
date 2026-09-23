# Multiple agents: what happens when two of them disagree

**What this measures is how the idempotency key is derived.** All three rows
below run identical agents against an identical gateway. The only thing that
changes is which fields go into the key, and that alone decides whether the
customer is refunded once or twice.

Under the shared scope the key is built from two things, and they do different
jobs:

```
shared:{coordination_id}:{tool_name}:{hash of selected arguments}
         ^ which work is shared              ^ what identifies it
```

**`coordination_id` is the enabling condition.** It names the work several agents
are collaborating on, a ticket here, and without it nothing converges across
sessions at all: the SDK refuses to derive a shared key, because a shared scope
with no domain would deduplicate two unrelated callers who happened to make the
same call. Every guarded row below sets it to the same ticket.

**Which arguments are hashed is the variable**, and it is the one thing this
folder changes between rows. Getting it wrong is what the `ticket + amount` row
shows.

The other folders here audit a real product at a pinned commit. This one
constructs the scenario, because none of the four codebases audited so far can
produce it: their shared work is a fetch or a refresh, where every caller wants
the identical thing and there is nothing to disagree about. That absence is
itself worth recording.

It covers **one** multi-agent failure, two agents reaching different conclusions
about the same work. The other two, several agents duplicating one action and
contention worsening as callers are added, are measured against real code in
[`../corsair/`](../corsair/).

Two agents look at ticket `TICKET-x`. Agent A concludes the refund is **40**.
Agent B concludes it is **35**. Neither is retrying the other and neither has
failed: they reasoned separately and reached different answers, which is what
non-deterministic reasoners do. The customer is owed one refund.

Each agent is its own OS process. They share no memory, no session, and no
channel to coordinate on.

| what the idempotency key is built from | refunds | total paid | what happened |
| :--- | :---: | :---: | :--- |
| nothing, no guard at all | 2.0 | 75 | **both agents refunded, every run** |
| **ticket + amount**, the default | 2.0 | 75 | **both agents refunded, every run** |
| **ticket only**, amount ignored | 1.0 | 35 or 40 | one refund, and nobody is told they disagreed |

Five runs per row. **Correct is one refund**, of either 40 or 35.

In the `ticket only` row, the amount paid is whichever agent won, and it changes
between runs.

## The finding: hashing the arguments does not deduplicate this

Read the `ticket + amount` row again. That is the guard you get without asking
for one.

Every idempotency library, this SDK included, derives a key by hashing what the
function was called with. You write `issueRefund({ ticket, amount })` and the key
is computed from both fields. Nobody chooses that; it is what happens when you
do not choose.

**Against two agents that disagree, it does nothing**, for a reason that is
obvious the moment it is written down:

```
agent A hashes  { ticket: "T-1", amount: 40 }  ->  key ...a3f1
agent B hashes  { ticket: "T-1", amount: 35 }  ->  key ...7c92
```

**Different amounts produce different keys.** The cache sees two unrelated
operations, grants both a lease, and both refund. The customer is out 75 on a
ticket owed one refund.

That is not a bug in the cache. It did exactly what it was asked: by every input
it was given, the two calls *are* different. The identity of the work was never
the arguments. It was the ticket.

## What fixes the duplication, and what it does not fix

The `ticket only` row builds the key from the ticket alone, deliberately leaving
the amount out of the hash:

```ts
tool(issueRefund, {
  scope: IdempotencyScope.SHARED,
  sharedOn: ["ticket"],   // the amount is NOT hashed
})
```

Both agents now derive one key, one refund happens, and the second agent adopts
the first's result. The duplication is closed.

**Look at what it cost.** The amount paid is 40 in some runs and 35 in others.
**Which agent wins is a coin flip**, decided by whichever process reached the
engine first. The customer gets a refund whose amount was chosen by a race.

And nobody is told. Not the agents, not the operator, not the ledger. The system
converged, correctly and silently, on an answer it had no basis for preferring.

## What nothing here does

**No arm reports the disagreement.** Including CellaFlow.

That is not an oversight in the harness, it is the current state of the product.
Sequence claims, the mechanism that does refuse a divergent caller, are keyed on
`(session_id, sequence)` and are therefore **session-scoped**. Two agents
spawned independently are in two sessions, so there is no shared position for a
claim to arbitrate. The only thing that spans sessions is the idempotency key,
and a key that has converged carries no record that two different proposals
reached it.

So the summary of the `ticket only` row is: **convergence is solved, reporting is
not.** A system that quietly picks one of two irreconcilable answers is better
than one that acts on both, and it is not the same as a system that knows they
disagreed.

## Which failure to build for first

Every other benchmark in this repository measures **duplication**: one agent
doing the same thing twice across a crash or a retry. This one measures
**disagreement**: two agents doing different things once each.

Anthropic's multi-agent research settles the priority between them. They found
agents are **low-variance and correlated**: 18 of 30 independently created a git
branch with the identical name, and "when one agent makes a bad decision, it is
likely that many agents will make that same bad decision."

**Correlated agents duplicate. They rarely contradict.** That points the same way
twice over:

- **Duplication is the common failure**, which is what the other three folders
  measure, and it gets worse as agents are added rather than better. Correlation
  clusters them into the same instant instead of spreading them out, which is the
  contention curve measured in [`../corsair/`](../corsair/).
- **Disagreement is the rarer one**, and the `ticket + amount` row shows the
  default guard has no answer for it at all. Rare and unguarded is a different
  risk profile from common and guarded, and worth knowing before choosing what to
  build.

This harness makes both measurable. It does not claim to know how often real
agents reach different conclusions, which is the one number nobody has.

## Run it

```bash
docker compose up -d --wait
npm install
npx tsx harness.ts
```

No API key, no model calls, about a minute. The agents are deterministic stand-ins
holding fixed conclusions, because the point is what the *infrastructure* does
when they differ, not whether a model would have differed.

Refunds are counted from `ledger.jsonl`, which the fake gateway appends and
fsyncs before returning. An agent cannot avoid a count by dying after refunding.

## Scope

- The disagreement is constructed, not observed. How often real agents reach
  different conclusions about the same work is exactly the open question, and
  nothing here measures it.
- Both agents are given one shot. Retries and crashes are covered by the other
  benchmarks in this repository.
- `sharedOn` requires choosing which arguments identify the work. That choice is
  the design decision this benchmark is really about, and getting it wrong in
  either direction either fails to converge or converges things that should not.
