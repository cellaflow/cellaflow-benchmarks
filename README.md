# cellaflow-benchmarks

What a crash or a race actually costs, measured against real agent codebases.

Each directory audits a published agent framework at a pinned commit, kills the
process at a chosen point, and counts what the retry does a second time. The
numbers come from a ledger rather than from the agent.

## The method, which is the point

Measuring this badly is easy, so the rules are fixed and the same everywhere:

1. **Audit real code at a pinned commit.** The agent, its graph, its prompts,
   its retry chain and its tool definitions are theirs. Nothing is
   reimplemented, and where a substitution is unavoidable it goes through the
   project's own documented extension points.
2. **Fake the external service, never the agent.** A fake model endpoint or a
   fake machine makes the run deterministic and free. The thing under test is
   untouched.
3. **Count from an fsynced ledger.** The fake service appends and fsyncs the
   record *before* it responds, so a process that dies the instant after acting
   is still counted as having acted. The agent is never asked what it did.
4. **The control row is a gate.** One run with no crash must perform each
   operation exactly once. If it does not, the harness says so and exits,
   because every other number would be a fault in the test rather than a
   finding about the subject.
5. **Repeat the rows.** Run counts are stated in each benchmark.

Rule 3 matters more than it looks. A study of
[25,930 agent episodes](https://arxiv.org/abs/2609.29095) found agents reported
success in 90% of the episodes in which they had duplicated an effect, so
anything graded on self-reported traces scores "duplicated and said it was fine"
as a pass.

## What is here

| benchmark | subject | pinned at | the finding |
| :--- | :--- | :--- | :--- |
| [`cua/`](cua/) | [Cua](https://github.com/trycua/cua) | `cua-agent` 0.9.0 | A crash mid-checkout charges the card twice. Measured on 0.8.4 and 0.9.0, identical. |
| [`corsair/`](corsair/) | [Corsair](https://github.com/corsairdev/corsair) | `4f268cdf` | Two replicas spend the same rotating refresh token. A crash leaves the stored credential broken, every run. |
| [`skyvern/`](skyvern/) | [Skyvern](https://github.com/Skyvern-AI/skyvern) | v1.0.53, `d23ceb4` | Seven crash and contention scenarios. Two end correctly. |
| [`inconvo/`](inconvo/) | [Inconvo](https://github.com/inconvoai/inconvo) | `fa63f29` | An interrupted answer costs 60% more in model calls. With leased tools, 7%. |
| [`multi-agent/`](multi-agent/) | constructed | n/a | Two agents reach different conclusions about one refund. How the key is derived decides whether the customer is refunded once or twice. |

Most directories carry a `with-cellaflow/` arm that runs the same harness with a
lease on each irreversible operation and reports which rows move.

## Two things stated up front

**One row never closes.** Where a crash lands between an effect happening and
the record of it committing, the effect is repeated and no lease prevents it.
The side effect and the record live in different systems. Every leased arm here
reports that row rather than hiding it, because a durable record shrinks the
exposure from the whole run to a single in-flight operation and does not reach
zero. A benchmark that crashes only at already-committed boundaries will report
zero duplicates while saying nothing about this case.

**Two caveats on the subjects.** `multi-agent/` constructs its scenario rather
than auditing a codebase, because none of the audited projects can produce it:
their shared work is a fetch or a refresh where every caller wants the identical
thing and there is nothing to disagree about. That absence is itself a finding.
And Inconvo joined [Attio](https://attio.com) after its audit was taken, so its
repository is now archived; the pinned commit is still public and still
reproduces.

## Running them

Each directory has its own instructions and dependencies. None of them need an
API key, a model account or a cloud provider, and none of them spend money on
inference: every model call goes to a local fake.

Docker is the only thing every directory wants, for the services an arm stands
up. Beyond that each has its own floor, because the subject sets it rather than
we do:

| benchmark | needs |
| :--- | :--- |
| [`cua/`](cua/) | Python 3.11 to 3.13; `cua-agent` does not support 3.14 |
| [`corsair/`](corsair/) | Node 18+, and a clone of Corsair |
| [`skyvern/`](skyvern/) | Python, a clone of Skyvern for its migrations, and Playwright's Chromium |
| [`inconvo/`](inconvo/) | Node 22+ and pnpm, and a clone of Inconvo |
| [`multi-agent/`](multi-agent/) | Node 18+ and nothing else |

Each subject is cloned into the benchmark's own directory, where the harness
looks for it by default. The `*_SRC` environment variable named in each README
overrides that if yours lives somewhere else.

Benchmarks that include a `with-cellaflow/` arm need the engine, which that
arm's `docker compose up -d --wait` starts. Those compose files set
`CELLAFLOW_ALLOW_UNAUTHENTICATED`, because the engine refuses to start with
neither a token nor that explicit opt-out. It is right for a benchmark bound to
loopback and wrong for anything reachable, so do not carry it elsewhere.

## Disclosure

Cellaflow builds the leasing runtime used in the `with-cellaflow/` arms, so this
repository is not a neutral party. The first arm of every benchmark is the
project's own code at a pinned commit, the harnesses are here, and the ledgers
are the raw output, so the most useful report we can receive is a number that
does not reproduce.
