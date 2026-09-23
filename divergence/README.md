# Two agents disagree. Deduplication does not notice.

**This one measures a primitive, not a company.** The other benchmarks here audit
real products at a pinned commit. This one constructs the scenario deliberately,
because none of the four codebases audited so far produces it, and that absence
is itself worth recording.

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

The third row's total is whichever agent won, and it changes between runs.

## The finding: the default idempotency key does not help here

The middle row is the one worth sitting with, and the mechanism is the whole
story.

An idempotency key derived from the tool's arguments is the standard answer,
what most libraries give by default and what a team writes first. **It fails
completely**, and it fails for a reason that is obvious once stated:

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

The third row builds the key from the ticket alone, deliberately leaving the
amount out of the hash:

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

So the honest summary of the third row is: **convergence is solved, reporting is
not.** A system that quietly picks one of two irreconcilable answers is better
than one that acts on both, and it is not the same as a system that knows they
disagreed.

## Why this benchmark exists

Every other benchmark in this repository measures **duplication**: one agent
doing the same thing twice across a crash or a retry. This is the only one
measuring **divergence**: two agents doing different things once each.

Worth stating plainly, because it cuts against the obvious reading: **divergence
may be the rarer failure.** Anthropic's multi-agent research found agents are
low-variance and converge rather than disagree, with 18 of 30 independently
choosing an identical git branch name, and concluded that "when one agent makes
a bad decision, it is likely that many agents will make that same bad decision."
Correlated agents duplicate. They do not often contradict.

If that holds, the middle row above is the important one and the third row's
silence matters less than it looks. If it does not hold, the third row is a
product gap. This harness makes the question concrete; it does not settle it.

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
