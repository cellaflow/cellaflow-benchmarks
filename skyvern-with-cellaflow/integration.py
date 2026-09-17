#!/usr/bin/env python3
"""The integration. Two leases, at two different layers, ~80 lines.

Skyvern's step guard (`agent_functions.py:1542`) asks whether any step on the
task is `running`. That answers "did someone start this", and it cannot answer
"is that someone still here" -- which is the only question that matters when a
process has died. A status column has no way to express liveness.

A heartbeated lease does, and that is the whole of what CellaFlow adds here:

  execution lease    keyed on the task. Held for the duration of one worker's
                     run and heartbeated while it lives. Holding it means every
                     `running` step on this task belongs to a holder that
                     stopped heartbeating -- a tombstone, not a live worker.
                     That is what makes clearing it safe, and nothing in
                     Skyvern can currently say it.

  operation lease    keyed on the business operation -- the order -- not on the
                     task, the step, or the run. Survives across tasks, so a
                     fresh run started after a crash finds the order already
                     accounted for instead of clicking a button it has no
                     memory of.

The execution lease uses the raw lease API rather than `@tool`, deliberately.
`@tool` commits a result on return, which is memoization; a second legitimate
step on the same task would take a cache hit and be skipped. Mutual exclusion
wants acquire/heartbeat/release with no commit, which is what this is.
"""

from __future__ import annotations

import contextlib
import os
import threading
from typing import Iterator

from cellaflow import CellaflowClient
from cellaflow.v1 import idempotency_pb2

ENGINE = os.environ.get("CELLAFLOW_TARGET", "localhost:50051")

# Short enough that a crashed worker's task frees up promptly; long enough that
# a heartbeat every third of it never races the expiry.
EXECUTION_LEASE_TTL_MS = 15_000
HEARTBEAT_INTERVAL_S = 5.0


class LeaseNotAcquired(RuntimeError):
    """Another worker holds this task and is still heartbeating."""


@contextlib.contextmanager
def task_execution_lease(task_id: str, worker_id: str) -> Iterator[int]:
    """Mutual exclusion over one task, with liveness.

    Yields the fencing token. Raises `LeaseNotAcquired` if a live worker holds
    it -- which is the case a status column reports identically to a dead one.
    """
    client = CellaflowClient(target=ENGINE)
    key = f"skyvern:task-execution:{task_id}"

    resp = client.check_idempotency_cache(
        agent_id=worker_id,
        idempotency_key=key,
        lease_ttl_ms=EXECUTION_LEASE_TTL_MS,
    )
    if resp.status != idempotency_pb2.CACHE_STATUS_ACQUIRED:
        raise LeaseNotAcquired(
            f"task {task_id} is held by a live worker (status={resp.status})"
        )

    token = resp.fencing_token
    stop = threading.Event()

    def heartbeat() -> None:
        # Renewing is what separates "slow" from "gone". Stop renewing -- by
        # dying, not by choosing to -- and the task becomes reclaimable.
        while not stop.wait(HEARTBEAT_INTERVAL_S):
            try:
                client.renew_lease(
                    agent_id=worker_id,
                    idempotency_key=key,
                    fencing_token=token,
                    extend_ms=EXECUTION_LEASE_TTL_MS,
                )
            except Exception:
                return

    beat = threading.Thread(target=heartbeat, daemon=True)
    beat.start()
    try:
        yield token
    finally:
        stop.set()
        with contextlib.suppress(Exception):
            client.release_lease(
                agent_id=worker_id, idempotency_key=key, fencing_token=token
            )
        with contextlib.suppress(Exception):
            client.close()


async def clear_stale_running_steps(app, task_id: str, organization_id: str) -> int:
    """Fail every `running` step on this task. Only safe while holding the lease.

    The lease is the entire justification. Without it this function is a race --
    the `running` step might belong to a worker that is alive and mid-click, and
    clearing it would let a second worker act on the same page. With it, the
    previous holder is provably gone.
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
