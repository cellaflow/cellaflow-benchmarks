# Every interrupted answer costs 60% more. CellaFlow brings that to 7%.

[Inconvo](https://github.com/inconvo/inconvo) is a conversational analytics
agent: a question comes in, a tree of sub-agents queries the database and reasons
over the result, and an answer comes back. This measures what one process death
costs, against their real graph at `fa63f29`.

Nothing about the agent tree is reimplemented. `inconvoAgent` is imported from the
clone and invoked the way their platform invokes it. The benchmark supplies two
things and nothing else: a fake model endpoint and a database connector.

## The result

```
                                  calls  retry  total  wasted
Inconvo alone
  control, one answer, no crash    15.0    0.0   15.0     0.0
  crash mid-answer, then retry      9.0   15.0   24.0     9.0

Inconvo + CellaFlow
  control, one answer, no crash    15.0    0.0   15.0     0.0
  crash mid-answer, then retry      7.0    9.0   16.0     1.0
```

Five runs per row.

**One answer costs fifteen model calls. A crash partway through makes that answer
cost twenty-four, which is 60% more for the same result. With leased tools and
durable results it costs sixteen, or 7% more.**

The 60% applies to each answer a crash interrupts, not to the whole bill. An
uninterrupted answer is unaffected, so the cost to a deployment is the crash rate
times the overhead: at a 4% interruption rate, roughly 2.4% of total model spend
buys nothing.

Nine model calls thrown away becomes one. Every retry under CellaFlow is handed
the work the first attempt already paid for, and pays only for what was never
finished.

The overhead is the headline number here because the answer itself is correct in
both arms. Inconvo's retry works. What it does not do is remember that the work
was already bought.

## Why the retry pays for everything again

`compile({ checkpointer })` appears **once** in the entire agents package, at
`packages/agents/src/inconvo/index.ts:1584`. Four sub-graphs compile with no
checkpointer at all:

```
database/index.ts:689                                                workflow.compile()
database/questionWhere/index.ts:233                                  workflow.compile()
database/operationParameters/index.ts:289                            workflow.compile()
database/operationParameters/utils/operationParametersAgent.ts:120   workflow.compile()
```

The checkpointer records that the outer node was entered. The fifteen model calls
underneath it leave no trace. A retry re-enters that node and starts from nothing.

The checkpointer is working correctly. It records at the granularity it was given,
and the expensive work happens below that granularity. Recording the graph is a
different job from recording what the graph spent, and a checkpointer only does
the first.

## What CellaFlow adds

Nothing in Inconvo changes. The audit driver already points `OPENAI_BASE_URL` at a
fake provider; this arm points it at a **leasing proxy** that forwards to the same
provider. Both arms run the same `driver.ts` and differ by one environment
variable.

Every model call passing through is leased and committed:

```ts
const leased = tool(async () => callUpstream(path, body), {
  idempotencyKey: `inconvo:model:${thread}:${digest}`,
  toolName: "model_call",
});
```

The key comes from the request body, not a counter. That is what makes recovery
work: a counter is position-dependent, and a crashed attempt does not resume at
the position it died at. Two attempts sending the same messages to the same model
are the same operation wherever they fall in the sequence, so the retry takes a
cache hit and is handed the committed result.

## Granularity: each call is its own operation

Inconvo fans out **concurrent** model calls, three in flight at the crash point.
Concurrent independent calls do not occupy a single ordered position in a graph,
and treating them as if they did produces `expected sequence 7, but request
specified 9`.

So each call gets its own single-step session, keyed on the request. The
idempotency cache is position-independent and cross-session by design, which is
exactly the property this workload needs: the question is never "which agent owns
this slot", it is "has this exact call already been made". Sequence claims answer
the first question and matter when replicas contend for one position. This
workload asks the second.

## The one call still lost

The crash row costs sixteen rather than fifteen. That call was in flight when the
process died: the provider had served it, and the process was gone before the
result committed.

That window is structural. It is the same one the [Corsair](../corsair/) and
[Cua](../cua/) benchmarks report, and it is bounded by a single call rather than
by the length of the answer. A durable record shrinks the exposure from the whole
run to one commit.

## Scope

The coarser boundary is `databaseRetrieverAgent`, where the five call sites in
this answer live. It is imported at module load by `inconvo/index.ts`, so leasing
at that boundary requires editing their source, and this benchmark runs their code
unmodified. The measurement here is per model call.

## How the crash lands mid-answer

The provider **parks** the nth call: it records it, fsyncs, then holds the socket
open so the driver blocks inside its `await`. The harness kills it there, with n
calls paid for and nothing returned.

Parking rather than polling is what makes the crash point exact. The fake provider
answers in microseconds, so a fifteen-call answer completes inside a single poll
interval and a watcher racing a `SIGKILL` always lands after the answer finished.

## What the harness supplies

Supplied: a fake OpenAI-compatible endpoint, and a fixture database connector.

Not supplied: the graph, the sub-agents, the prompts, the tool definitions, or the
checkpointer wiring. Model calls are counted from the provider's own
append-and-fsync ledger, written before any response is sent. The agent tree is
never asked how much work it did.

## Run it

```bash
git clone https://github.com/inconvo/inconvo
cd inconvo && git checkout fa63f29
corepack pnpm install --filter "@repo/agents..."
```

Point `INCONVO_SRC` in `config.ts` at that clone. Their `package.json` requires
node >= 22 and pins pnpm 11.1.2.

```bash
npm install
npx tsx harness.ts                      # Inconvo alone

cd with-cellaflow
docker compose up -d --wait             # the engine, the only service
npm install
npx tsx harness.ts                      # Inconvo + CellaFlow
```

No OpenAI account and no model spend: every call goes to the local fake provider.
