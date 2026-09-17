#!/usr/bin/env python3
"""Runs every scenario against real Skyvern and prints the table.

Each scenario spawns `driver.py` as a subprocess so a crash is a real process
death, and counts orders from `ledger.jsonl`, which the checkout server writes
and fsyncs from inside the POST handler. Nothing self-reports.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import uuid

HERE = pathlib.Path(__file__).parent
LEDGER = HERE / "ledger.jsonl"
PY = str(HERE / "venv" / "bin" / "python") if (HERE / "venv").exists() else sys.executable
CHECKOUT_BASE = os.environ.get("BENCH_CHECKOUT_URL", "http://127.0.0.1:8899/")

IDS = re.compile(r"\[driver\] IDS org=(\S+) task=(\S+) step=(\S+)")


def orders(order_id: str) -> int:
    if not LEDGER.exists():
        return 0
    return sum(
        1 for line in LEDGER.read_text().splitlines()
        if line.strip() and json.loads(line)["order_id"] == order_id
    )


def run(order_id: str, *, die: str = "no", org: str = "", task: str = "",
        setup_only: bool = False, plan: str = "single", background: bool = False):
    env = {
        **os.environ,
        "BENCH_ORDER_ID": order_id,
        "BENCH_CHECKOUT_URL": f"{CHECKOUT_BASE}?order={order_id}",
        "BENCH_DIE_AT": die,
        "BENCH_PLAN": plan,
        "BENCH_ORG_ID": org,
        "BENCH_TASK_ID": task,
        "BENCH_SETUP_ONLY": "1" if setup_only else "",
    }
    kw = dict(env=env, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if background:
        return subprocess.Popen([PY, "driver.py"], **kw)
    return subprocess.run([PY, "driver.py"], **kw)


def ids_from(output: str) -> tuple[str, str]:
    m = IDS.search(output)
    return (m.group(1), m.group(2)) if m else ("", "")


def blocked(output: str) -> bool:
    return "another_step_is_running_for_task" in output


# ---------------------------------------------------------------------------

def sc_control() -> tuple[int, str]:
    oid = f"ORD-{uuid.uuid4().hex[:6]}"
    run(oid)
    return orders(oid), "correct"


def sc_crash_then_retry(die: str) -> tuple[int, str]:
    oid = f"ORD-{uuid.uuid4().hex[:6]}"
    first = run(oid, die=die)
    org, task = ids_from(first.stdout)
    second = run(oid, org=org, task=task)
    return orders(oid), "task stranded" if blocked(second.stdout) else "retried"


def sc_batch_fail() -> tuple[int, str]:
    oid = f"ORD-{uuid.uuid4().hex[:6]}"
    run(oid, plan="batch_then_fail")
    return orders(oid), "step completes, no retry"


def sc_new_task_after_deadlock() -> tuple[int, str]:
    """The stranded task cannot be retried, so a new run is started -- which is
    what an operator or the customer does. A new task has no action history."""
    oid = f"ORD-{uuid.uuid4().hex[:6]}"
    run(oid, die="after_click")
    run(oid)                       # a brand new task, same order
    return orders(oid), "fresh run re-orders"


def sc_race() -> tuple[int, str]:
    """Two processes on one task that has been created but not yet executed."""
    oid = f"ORD-{uuid.uuid4().hex[:6]}"
    seed = run(oid, setup_only=True)
    org, task = ids_from(seed.stdout)
    procs = [run(oid, org=org, task=task, background=True) for _ in range(2)]
    for p in procs:
        p.communicate(timeout=300)
    return orders(oid), "both processes act"


def ops(order_id: str) -> dict:
    """Per-operation counts. `orders` is left untouched so the published six
    rows keep the counter they were measured with."""
    out: dict = {}
    if not LEDGER.exists():
        return out
    for line in LEDGER.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec["order_id"] == order_id:
            out[rec.get("op", "place-order")] = out.get(rec.get("op", "place-order"), 0) + 1
    return out


def sc_multi_op() -> tuple[dict, str]:
    """Three irreversible operations in one batch; the process dies between the
    second and the third; the same task is retried."""
    oid = f"ORD-{uuid.uuid4().hex[:6]}"
    first = run(oid, die="between_ops", plan="multi_op")
    org, task = ids_from(first.stdout)
    second = run(oid, org=org, task=task, plan="multi_op")
    got = ops(oid)
    note = "retry blocked" if blocked(second.stdout) else "retry ran"
    return got, note


SCENARIOS = [
    ("control, no crash",                 1, sc_control),
    ("crash before the click, then retry", 1, lambda: sc_crash_then_retry("before_click")),
    ("crash after the click, then retry",  1, lambda: sc_crash_then_retry("after_click")),
    ("action fails mid-batch",             1, sc_batch_fail),
    ("stranded task, operator reruns",     1, sc_new_task_after_deadlock),
    ("two processes, one task",            1, sc_race),
]


def main() -> int:
    if LEDGER.exists():
        LEDGER.unlink()

    print()
    print("  Skyvern v1.0.53 (d23ceb4) -- real execute_step, Playwright, Postgres.")
    print("  The LLM is replaced by a canned planner; nothing else is substituted.")
    print()
    print(f"  {'scenario':<36}{'orders':>8}{'correct':>9}   outcome")
    print("  " + "-" * 74)

    got_multi, multi_note = sc_multi_op()

    control_ok = True
    for i, (name, expected, fn) in enumerate(SCENARIOS):
        got, outcome = fn()
        flag = "" if got == expected else "  <-- "
        if i == 0 and got != expected:
            control_ok = False
        print(f"  {name:<36}{got:>8}{expected:>9}   {outcome}{flag}")

    print()
    print(f"  {'three operations, crash between 2 and 3':<36}{'':>8}{'':>9}   {multi_note}")
    for op in ("reserve", "charge", "confirm"):
        n = got_multi.get(op, 0)
        want = 1
        flag = "" if n == want else "  <-- "
        print(f"    {op:<34}{n:>8}{want:>9}   {flag}")

    print()
    if not control_ok:
        print("  Control must place exactly one order. It did not, so treat every")
        print("  other row as a harness fault.")
        return 1
    print("  Rows marked <-- place a different number of orders than correctness allows.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
