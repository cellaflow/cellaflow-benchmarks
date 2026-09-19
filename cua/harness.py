#!/usr/bin/env python3
"""Runs every scenario against Cua's real agent loop and prints the table.

Each run is a subprocess so a crash is a real process death. Operations are
counted from ledger.jsonl, which the fake machine appends and fsyncs before
returning. Nothing self-reports.
"""
from __future__ import annotations
import json, os, pathlib, subprocess, sys, uuid

HERE = pathlib.Path(__file__).parent
LEDGER = HERE / "ledger.jsonl"
PY = str(HERE / "venv" / "bin" / "python")
OPS = ("reserve", "charge", "confirm")


def counts(order_id: str) -> dict:
    out: dict = {}
    if not LEDGER.exists():
        return out
    for line in LEDGER.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            if r["order_id"] == order_id:
                out[r["op"]] = out.get(r["op"], 0) + 1
    return out


def run(order_id: str, *, die_after: str = "", die_between: str = "") -> None:
    env = {**os.environ, "BENCH_ORDER_ID": order_id,
           "BENCH_DIE_AFTER": die_after, "BENCH_DIE_BETWEEN": die_between}
    subprocess.run([PY, "driver.py"], env=env, cwd=HERE,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def scenario(name: str, **kw) -> tuple[str, dict]:
    oid = f"ORD-{uuid.uuid4().hex[:6]}"
    run(oid, **kw)
    if kw:                      # a crash happened; a supervisor retries
        run(oid)
    return name, counts(oid)


def main() -> int:
    if LEDGER.exists():
        LEDGER.unlink()
    arm = "Cua + operation leases" if (HERE / "checkout.py").read_text().find("idempotency_key") > 0 else "Cua"

    print()
    print(f"  {arm} -- cua-agent 0.8.4, real agent loop, canned model, fake machine.")
    print("  Operations performed for one order id; 1 is correct everywhere.")
    print()
    print(f"  {'scenario':<44}{'reserve':>9}{'charge':>8}{'confirm':>9}")
    print("  " + "-" * 68)

    rows = [scenario("control, no crash")]
    rows.append(scenario("crash after an operation, then retry", die_after="charge"))
    rows.append(scenario("crash between operations, then retry", die_between="charge"))

    bad = 0
    for name, c in rows:
        flag = "" if all(c.get(o, 0) == 1 for o in OPS) else "  <--"
        if flag:
            bad += 1
        print(f"  {name:<44}{c.get('reserve',0):>9}{c.get('charge',0):>8}{c.get('confirm',0):>9}{flag}")

    print()
    if rows[0][1] != {o: 1 for o in OPS}:
        print("  Control did not perform each operation exactly once. Every other")
        print("  row is a harness fault until that is fixed.")
        return 1
    print("  Rows marked <-- perform an operation a different number of times than")
    print("  correctness allows.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
