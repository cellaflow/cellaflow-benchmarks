#!/usr/bin/env python3
"""Runs the whole matrix and prints the tables.

Every cell is five runs. Each run resets the ERP, gives the ten emails fresh
purchase order numbers, executes the scenario in real subprocesses, and then
asks the ERP how many Sales Orders exist against each of those numbers. The
agents are never asked what they did.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import extract
from erp import Erp
from extract import inbox

HERE = pathlib.Path(__file__).parent
JOBS = HERE / "jobs"
PY_BIN = sys.executable
RUNS = int(os.environ.get("BENCH_RUNS", "5"))

# The sixth of the ten emails. Late enough that five jobs are already finished
# and recorded when the process dies, early enough that four are still to come,
# so a run that loses its memory loses something visible in both directions.
CRASH_ON_EMAIL = "po-0006.txt"

# One cell is one (scenario, arm) pair. Both can be narrowed from the
# environment so a single cell can be re-run without the other twenty-three.
ARMS = [a for a in ["baseline", "guard", "builtin", "cellaflow"]
        if a in (os.environ.get("BENCH_ARMS") or "baseline,guard,builtin,cellaflow").split(",")]
ARM_LABELS = {
    "baseline": "A. baseline",
    "guard": "B. query-first guard",
    "builtin": "B2. ERPNext setting",
    "cellaflow": "C. leased write",
}


@dataclass
class Scenario:
    key: str
    label: str
    crash_at: str = ""
    fault: str = ""
    two_workers: bool = False


SCENARIOS = [
    Scenario("control", "control, no crash"),
    Scenario("in-write", "crash after the write, before any record of it", crash_at="in-write"),
    Scenario("after-write", "crash after the record, before the job is marked done", crash_at="after-write"),
    Scenario("before-write", "crash before the write", crash_at="before-write"),
    Scenario("two-workers", "two workers take the same inbox", two_workers=True),
    Scenario("lost-response", "the ERP commits, the answer never arrives", fault="lose-response"),
]


_WANTED = os.environ.get("BENCH_SCENARIOS")

@dataclass
class RunResult:
    duplicates: int = 0
    missing: int = 0
    refused: int = 0
    reused_after: bool = False
    # Purchase order number -> the docstatus of every Sales Order carrying it.
    # 1 is submitted, 0 is a draft. The distinction matters: two submitted
    # orders are two commitments, while a submitted order beside an abandoned
    # draft is one commitment and some tidying up.
    states: Dict[str, List[int]] = field(default_factory=dict)
    transcript: str = ""

    @property
    def clean(self) -> bool:
        return self.duplicates == 0 and self.missing == 0

    @property
    def duplicate_commitments(self) -> int:
        """Purchase orders carrying more than one submitted Sales Order."""
        return sum(1 for s in self.states.values() if s.count(1) > 1)

    @property
    def stray_drafts(self) -> int:
        """Purchase orders left with an unsubmitted duplicate beside the real one."""
        return sum(1 for s in self.states.values() if s.count(1) == 1 and s.count(0) >= 1)


def spawn(arm: str, run_tag: str, jobs_db: pathlib.Path, session: str, *,
          crash_at: str = "", crash_on: str = "", fault: str = "", fault_on: str = "",
          start_at: float = 0.0, wait: bool = True):
    env = {
        **os.environ,
        "BENCH_ARM": arm,
        "BENCH_RUN_TAG": run_tag,
        "BENCH_JOBS_DB": str(jobs_db),
        "BENCH_SESSION": session,
        "BENCH_CRASH_AT": crash_at,
        "BENCH_CRASH_ON": crash_on,
        "BENCH_FAULT": fault,
        "BENCH_FAULT_ON": fault_on,
        "BENCH_START_AT": str(start_at),
    }
    proc = subprocess.Popen(
        [PY_BIN, "driver.py"], env=env, cwd=HERE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    if not wait:
        return proc
    out, _ = proc.communicate()
    return out


def one_run(erp: Erp, scenario: Scenario, arm: str) -> RunResult:
    run_tag = uuid.uuid4().hex[:8]
    # Both, and for different readers. The environment variable reaches the
    # worker subprocesses; the module global is what this process uses to
    # derive the same purchase order numbers it is about to count. Setting
    # only the first is a silent miscount: the workers tag their orders and
    # the harness looks for untagged ones.
    os.environ["BENCH_RUN_TAG"] = run_tag
    extract.RUN_TAG = run_tag
    orders = inbox()
    by_email = {o.email: o for o in orders}
    crash_po = by_email[CRASH_ON_EMAIL].po_no

    erp.reset()
    # Arm B2 is the only one that leaves ERPNext's own refusal switched on.
    erp.set_allow_multiple_po(arm != "builtin")

    if JOBS.exists():
        shutil.rmtree(JOBS)
    JOBS.mkdir(parents=True)

    transcript = ""
    if scenario.two_workers:
        start = time.time() + 2.0
        procs = [
            spawn(arm, run_tag, JOBS / f"w{i}.db", f"worker-{i}-{run_tag}",
                  start_at=start, wait=False)
            for i in (1, 2)
        ]
        for p in procs:
            out, _ = p.communicate()
            transcript += out
    elif scenario.crash_at:
        transcript += spawn(arm, run_tag, JOBS / "w1.db", f"pass1-{run_tag}",
                            crash_at=scenario.crash_at, crash_on=crash_po)
        # A supervisor notices the worker is gone and starts it again. This is
        # the second pass, and it is where every finding in this benchmark
        # either happens or does not.
        transcript += "--- retry ---\n"
        retry = spawn(arm, run_tag, JOBS / "w1.db", f"pass2-{run_tag}")
        transcript += retry
    elif scenario.fault:
        transcript += spawn(arm, run_tag, JOBS / "w1.db", f"pass1-{run_tag}",
                            fault=scenario.fault, fault_on=crash_po)
        transcript += "--- retry ---\n"
        transcript += spawn(arm, run_tag, JOBS / "w1.db", f"pass2-{run_tag}")
    else:
        transcript += spawn(arm, run_tag, JOBS / "w1.db", f"pass1-{run_tag}")

    states = {o.po_no: [so["docstatus"] for so in erp.orders_for(o.po_no)] for o in orders}
    after_marker = transcript.split("--- retry ---")[-1]
    return RunResult(
        duplicates=sum(1 for s in states.values() if len(s) > 1),
        missing=sum(1 for s in states.values() if not s),
        refused=transcript.count("refused: duplicate purchase order"),
        reused_after=f"{crash_po}: reused" in after_marker,
        states=states,
        transcript=transcript,
    )


def main() -> int:
    if _WANTED:
        wanted = _WANTED.split(",")
        SCENARIOS[:] = [s for s in SCENARIOS if s.key in wanted]
        if not SCENARIOS:
            raise SystemExit(f"No scenario named in BENCH_SCENARIOS={_WANTED}")
    erp = Erp()
    results: Dict[str, Dict[str, List[RunResult]]] = {}

    print()
    print("  An agent turning emailed purchase orders into ERPNext sales orders.")
    print(f"  Ten emails per run, {RUNS} runs per cell, ERP reset between runs.")
    print("  Counted by asking ERPNext how many Sales Orders carry each PO number.")
    print()

    for scenario in SCENARIOS:
        results[scenario.key] = {}
        for arm in ARMS:
            runs = []
            for i in range(RUNS):
                runs.append(one_run(erp, scenario, arm))
                # Progress goes to stderr so that redirecting stdout to a file
                # captures the tables and nothing else.
                print(f"  ...{scenario.key:<14} {arm:<10} run {i + 1}/{RUNS}",
                      end="\r", file=sys.stderr, flush=True)
            results[scenario.key][arm] = runs

    print(" " * 70, end="\r", file=sys.stderr)

    # Gate. A control row that is not clean everywhere makes every other number
    # a fault in the test rather than a finding about the arms.
    control = results.get("control", {})
    broken = [a for a in ARMS if control.get(a) and not all(r.clean for r in control[a])]
    if broken:
        print()
        for a in broken:
            for r in control[a]:
                if not r.clean:
                    print(f"  control, {a}: {r.duplicates} duplicated, {r.missing} missing")
                    print("  " + r.transcript.replace("\n", "\n  ")[:2000])
        print("\n  The control row did not produce exactly one order per email in every")
        print("  arm. Every other row is a harness fault until that is fixed.")
        return 1

    width = 52
    print(f"  {'duplicate orders, runs out of ' + str(RUNS):<{width}}" +
          "".join(f"{ARM_LABELS[a]:>22}" for a in ARMS))
    print("  " + "-" * (width + 22 * len(ARMS)))
    for scenario in SCENARIOS:
        cells = []
        for arm in ARMS:
            runs = results[scenario.key][arm]
            d = sum(1 for r in runs if r.duplicates)
            cells.append(f"{d}/{len(runs)}")
        print(f"  {scenario.label:<{width}}" + "".join(f"{c:>22}" for c in cells))

    print()
    print(f"  {'orders never created, runs out of ' + str(RUNS):<{width}}" +
          "".join(f"{ARM_LABELS[a]:>22}" for a in ARMS))
    print("  " + "-" * (width + 22 * len(ARMS)))
    for scenario in SCENARIOS:
        cells = []
        for arm in ARMS:
            runs = results[scenario.key][arm]
            m = sum(1 for r in runs if r.missing)
            cells.append(f"{m}/{len(runs)}")
        print(f"  {scenario.label:<{width}}" + "".join(f"{c:>22}" for c in cells))

    print()
    print(f"  {'of which two SUBMITTED orders, runs out of ' + str(RUNS):<{width}}" +
          "".join(f"{ARM_LABELS[a]:>22}" for a in ARMS))
    print("  " + "-" * (width + 22 * len(ARMS)))
    for scenario in SCENARIOS:
        cells = []
        for arm in ARMS:
            runs = results[scenario.key][arm]
            d = sum(1 for r in runs if r.duplicate_commitments)
            cells.append(f"{d}/{len(runs)}")
        print(f"  {scenario.label:<{width}}" + "".join(f"{c:>22}" for c in cells))

    print()
    print("  What the second attempt was handed back")
    print("  " + "-" * 74)
    for scenario in SCENARIOS:
        if scenario.key == "control":
            continue
        for arm in ARMS:
            runs = results[scenario.key][arm]
            reused = sum(1 for r in runs if r.reused_after)
            refused = sum(r.refused for r in runs)
            note = []
            if reused:
                note.append(f"given the original order {reused}/{len(runs)}")
            if refused:
                note.append(f"refused by the ERP {refused}x")
            if note:
                print(f"  {scenario.key:<16}{ARM_LABELS[arm]:<24}{', '.join(note)}")

    out = HERE / "results.json"
    out.write_text(json.dumps(
        {
            s.key: {
                a: [
                    {"duplicates": r.duplicates, "missing": r.missing,
                     "refused": r.refused, "reused_after": r.reused_after,
                     "duplicate_commitments": r.duplicate_commitments,
                     "stray_drafts": r.stray_drafts,
                     "orders_per_po": r.states}
                    for r in results[s.key][a]
                ]
                for a in ARMS
            }
            for s in SCENARIOS
        }, indent=2) + "\n")
    print(f"\n  Raw per-run counts in {out.name}.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
