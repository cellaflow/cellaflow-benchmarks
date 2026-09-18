#!/usr/bin/env python3
"""The integration. Two leases at two layers, and one function of our own.

Skyvern's step guard (`agent_functions.py:1542`) asks whether any step on the
task is `running`. That answers *did someone start this*. It cannot answer *is
that someone still here*, which is the only question that matters once a process
has died — a status column has no way to express liveness.

A heartbeated lease can, and both halves come from the SDK:

  execution lease    `async_execution_lease`, keyed on the task. Heartbeated
                     while the worker lives, so holding it means every `running`
                     step on that task belongs to a holder that stopped
                     heartbeating — a tombstone, not a live worker. That is what
                     makes clearing it safe.

  operation lease    `durable_tools` + `@tool`, keyed on the business operation
                     rather than the task, step or run. The idempotency cache is
                     position-independent, so a retry under a new step id — or a
                     fresh task entirely — derives the same key and takes a hit.

`clear_stale_running_steps` below is the only code this integration adds beyond
calling those two. It is short because the lease has already answered the hard
part.
"""

from __future__ import annotations

import os

ENGINE = os.environ.get("CELLAFLOW_TARGET", "localhost:50051")

# 15s TTL with a 5s heartbeat are the SDK defaults; named here because the row
# that matters depends on them. A crashed worker's task becomes reclaimable one
# TTL after its last heartbeat, and a worker that is merely slow keeps renewing
# and keeps its task.
LEASE_TTL_MS = 15_000
HEARTBEAT_MS = 5_000


async def clear_stale_running_steps(app, task_id: str, organization_id: str) -> int:
    """Fail every `running` step on this task. Only sound while holding the lease.

    The lease is the entire justification. Without it this is a race: the
    `running` step might belong to a worker that is alive and mid-click, and
    clearing it would let a second worker act on the same page. With it, the
    previous holder is provably gone — it stopped heartbeating, which is a thing
    only a dead or partitioned process does.
    """
    from skyvern.forge.sdk.models import StepStatus

    steps = await app.DATABASE.tasks.get_task_steps(
        task_id=task_id, organization_id=organization_id
    )
    cleared = 0
    for step in steps:
        if step.status == StepStatus.running:
            await app.DATABASE.tasks.update_step(
                task_id=task_id,
                step_id=step.step_id,
                organization_id=organization_id,
                status=StepStatus.failed,
            )
            cleared += 1
    return cleared
