#!/usr/bin/env python3
"""Duplicate browser side effects under crash and retry.

Reproduces the action-execution ordering in Skyvern's agent loop
(`skyvern/forge/agent.py`, commit d23ceb4, v1.0.53) and measures what a retry
does to an irreversible action.

The ordering being reproduced, from the source:

    4288:  results = await ActionHandler.handle_action(...)   # the click happens
    4299:  detailed_agent_step_output.actions_and_results[action_idx] = (action, results)
           # ^ in-memory only; the step's output is persisted after the action loop

There is no `create_action` before `handle_action` on this path. (There is one
at 4152, but that is the internal-refresh branch, which `break`s before
reaching the dispatch above.)

So between the action landing on the page and the step being persisted there is
a window. A process that dies inside it leaves the external world moved and
nothing durable saying so, and Skyvern's own retry (`step.retry_index`,
`max_retries_per_step`, agent.py:8548-8626) re-runs the step.

WHAT THIS IS NOT
----------------
This does not run Skyvern. Skyvern's unit of work is an LLM deciding what to
click, which is nondeterministic, needs API keys and a browser, and cannot be
reproduced by a reader. What this runs is the *ordering*, against a fake
checkout endpoint that records every purchase to an append-only ledger.

That makes the result a statement about the ordering, not a measurement of
Skyvern's end-to-end behaviour. The ordering is cited above so it can be
checked in ten seconds. If it is wrong, the result is worthless and we would
like to know.

The ledger adjudicates. No arm reports its own success.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import pathlib
import sys
import time
import uuid
from typing import Callable, Optional

LEDGER = pathlib.Path(__file__).parent / "ledger.jsonl"
ENGINE = os.environ.get("CELLAFLOW_TARGET", "localhost:50051")


# ---------------------------------------------------------------------------
# The irreversible side effect, and the only thing that decides the result.
# ---------------------------------------------------------------------------

def place_order(order_id: str, actor: str) -> str:
    """Stands in for `ActionHandler.handle_action` on a Click("Place order").

    Appends to the ledger from *inside* the effect, before returning, so a
    process that dies immediately after the click is still counted as having
    made it. That is the whole point: an arm cannot avoid a charge by dying
    before it reports one.
    """
    confirmation = f"conf_{uuid.uuid4().hex[:10]}"
    with LEDGER.open("a") as fh:
        fh.write(json.dumps({
            "order_id": order_id,
            "confirmation": confirmation,
            "actor": actor,
            "at": time.time(),
        }) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return confirmation


def orders_placed(order_id: str) -> int:
    if not LEDGER.exists():
        return 0
    n = 0
    for line in LEDGER.read_text().splitlines():
        if line.strip() and json.loads(line)["order_id"] == order_id:
            n += 1
    return n


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------

def arm_skyvern_shaped(order_id: str, run_id: str, die: str) -> None:
    """Skyvern's ordering: execute, hold the result in memory, persist later.

    `step_output` is the in-memory `actions_and_results` entry; `persist_step`
    is the write that happens after the action loop.

    Both crash points are the same place here, because there is nothing between
    the action and `persist_step` to crash between. That is the finding.
    """
    step_output = place_order(order_id, actor=run_id)      # agent.py:4288

    if die in ("during", "after_record"):
        os._exit(137)

    persist_step(run_id, step_output)                       # after the loop


def arm_cellaflow(order_id: str, run_id: str, die: str) -> None:
    """The same ordering, with the action leased.

    The two crash points are genuinely different here, which is the only
    reason this arm can do better -- and the only reason it still cannot do
    better on `during`:

      during       -- inside the tool, after the effect, BEFORE the engine
                      commit. Nothing anywhere records the effect. Identical
                      in kind to the skyvern-shaped arm, and it loses the same
                      way. No guard closes this window; see the README.
      after_record -- after the engine commit, before the local step write.
                      The operation is already durable off-process, so the
                      retry finds it.
    """
    from cellaflow import durable_tools, tool

    @tool(idempotency_key=f"place_order:{order_id}")
    def leased_place_order() -> str:
        conf = place_order(order_id, actor=run_id)
        if die == "during":
            # Before the decorator commits the result to the engine.
            os._exit(137)
        return conf

    with durable_tools(f"skyvern-bench-{order_id}", target=ENGINE):
        step_output = leased_place_order()

        if die == "after_record":
            os._exit(137)

        persist_step(run_id, step_output)


ARMS: dict[str, Callable[[str, str, str], None]] = {
    "skyvern-shaped": arm_skyvern_shaped,
    "cellaflow": arm_cellaflow,
}


def persist_step(run_id: str, output: str) -> None:
    """Stands in for the post-action-loop step write. Deliberately trivial:
    the benchmark is about *when* it happens, not what it stores."""
    path = pathlib.Path(__file__).parent / ".steps"
    path.mkdir(exist_ok=True)
    (path / f"{run_id}.json").write_text(json.dumps({"output": output}))


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def _run(arm: str, order_id: str, run_id: str, die: str) -> None:
    ARMS[arm](order_id, run_id, die)


def _spawn(arm: str, order_id: str, run_id: str, die: str) -> int:
    ctx = multiprocessing.get_context("spawn")
    p = ctx.Process(target=_run, args=(arm, order_id, run_id, die))
    p.start()
    p.join(120)
    return p.exitcode if p.exitcode is not None else -1


def scenario_control(arm: str) -> int:
    """N=1, nobody dies. Must be exactly 1 under every arm.

    If this is not 1, nothing else in this file means anything.
    """
    order = f"ORD-{uuid.uuid4().hex[:6]}"
    _spawn(arm, order, "run-1", die="no")
    return orders_placed(order)


def scenario_crash_then_retry(arm: str, die: str) -> int:
    """One run dies at `die`; a second run retries the same order."""
    order = f"ORD-{uuid.uuid4().hex[:6]}"
    _spawn(arm, order, "run-1", die=die)
    _spawn(arm, order, "run-2", die="no")
    return orders_placed(order)


def scenario_concurrent(arm: str, n: int) -> int:
    """N runs race on one order. Models a redelivered webhook or a
    double-submitted task, both of which Skyvern's create-time idempotency key
    covers at the API boundary but not inside the run."""
    order = f"ORD-{uuid.uuid4().hex[:6]}"
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_run, args=(arm, order, f"run-{i}", "no"))
        for i in range(n)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
    return orders_placed(order)


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--writers", default="5", help="concurrency level(s), comma separated")
    args = ap.parse_args()

    arms = [a.strip() for a in args.arms.split(",")]
    levels = [int(x) for x in args.writers.split(",")]

    if LEDGER.exists():
        LEDGER.unlink()

    print()
    print("  Skyvern action-ordering benchmark")
    print("  reproduces skyvern/forge/agent.py:4288 (commit d23ceb4, v1.0.53)")
    print("  orders placed for one order id; 1 is correct everywhere")
    print()

    header = f"  {'arm':<16}{'control':>9}{'crash: during':>15}{'crash: after record':>21}"
    for n in levels:
        header += f"{str(n) + ' race':>10}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    failed_control = False
    for arm in arms:
        control = scenario_control(arm)
        if control != 1:
            failed_control = True
        row = (f"  {arm:<16}{control:>9}"
               f"{scenario_crash_then_retry(arm, 'during'):>15}"
               f"{scenario_crash_then_retry(arm, 'after_record'):>21}")
        for n in levels:
            row += f"{scenario_concurrent(arm, n):>10}"
        print(row)

    print()
    if failed_control:
        print("  CONTROL FAILED. An arm did not place exactly one order with nobody")
        print("  dying and nobody racing. Treat every other number here as a harness")
        print("  bug until that is fixed.")
        return 1

    print("  control       = 1 worker, no crash, no race. Any value but 1 is a harness bug.")
    print("  crash: during = dies between the action and ANY durable record of it.")
    print("                  Nothing here closes this window, ours included.")
    print("  after record  = dies after the operation is durably recorded, before the")
    print("                  local step write. This is the window the ordering opens.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
