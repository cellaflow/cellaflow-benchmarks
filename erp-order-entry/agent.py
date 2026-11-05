#!/usr/bin/env python3
"""The agent: read an email, extract the order, write it to the ERP, record it.

The three arms differ in one function, `write_order`, and in nothing else. The
loop, the extraction, the job table, the crash points and the ERP client are
shared, so a difference in the results table is a difference in how the write
is guarded and cannot be anything else.
"""

from __future__ import annotations

import os
import pathlib
from typing import Any, Callable, Dict

from erp import Erp, ErpError, ResponseLost
from extract import Order
from jobstate import JobStore

# Where to die, and on which purchase order. Read once so the crash point is a
# property of the run rather than something the agent can drift into.
CRASH_AT = os.environ.get("BENCH_CRASH_AT", "")
CRASH_ON = os.environ.get("BENCH_CRASH_ON", "")
ENGINE = os.environ.get("CELLAFLOW_TARGET", "localhost:50051")


def _die(point: str, po_no: str) -> None:
    """Kill the process, with no cleanup and no chance to flush anything.

    `os._exit` skips atexit handlers, `finally` blocks, buffered writes and
    destructors, which is the point: a worker whose pod is evicted or whose
    host loses power gets none of those either. A `sys.exit` here would let the
    agent tidy up and would measure something kinder than a crash.
    """
    if point == CRASH_AT and po_no == CRASH_ON:
        print(f"[agent] dying at {point} on {po_no}", flush=True)
        os._exit(137)


# --- the three arms ------------------------------------------------------


def write_baseline(order: Order, erp: Erp) -> Dict[str, Any]:
    """Arm A. Create the order. Nothing guards it."""
    result = erp.create_sales_order(order)
    _die("in-write", order.po_no)
    return result


def write_guarded(order: Order, erp: Erp) -> Dict[str, Any]:
    """Arm B. Ask the ERP whether this PO is already there, and skip if so.

    This is what a competent team writes first, and against this ERP it is a
    reasonable thing to write: `po_no` is a field the REST API will filter on,
    so the question can actually be asked. It carries no index, so asking it
    costs a table scan, which is also what ERPNext's own check pays. The two
    failures it has are structural rather than sloppy. The check and the write
    are two separate requests, so two workers can both pass the check before
    either writes; and
    it only works at all because this particular destination happens to be
    queryable by the business key, which most of the calls an agent makes are
    not.
    """
    existing = erp.orders_for(order.po_no)
    if existing:
        return {
            "name": existing[0]["name"],
            "po_no": order.po_no,
            "customer": order.customer,
            "reused": True,
        }
    result = erp.create_sales_order(order)
    _die("in-write", order.po_no)
    return result


def write_leased(order: Order, erp: Erp) -> Dict[str, Any]:
    """Arm C. The same write, leased on the customer's purchase order number.

    The key is passed explicitly, so it is derived from the work and from
    nothing else: not the session, not the process, not the position in the
    run, not the extracted line items. A retry in a fresh process derives the
    same string, and so does a second worker that never shared memory with the
    first. That is what makes the result reach them.

    The crash point sits inside the decorated function, after the ERP has
    committed and before the lease has. That is the window nothing closes, and
    putting it anywhere more flattering would be the easiest way to make this
    arm look better than it is.
    """
    from cellaflow import tool

    @tool(idempotency_key=f"sales-order:{order.po_no}")
    def create(po_no: str) -> Dict[str, Any]:
        result = erp.create_sales_order(order)
        _die("in-write", po_no)
        return result

    return create(order.po_no)


ARMS: Dict[str, Callable[[Order, Erp], Dict[str, Any]]] = {
    "baseline": write_baseline,
    "guard": write_guarded,
    "builtin": write_baseline,   # arm B2: the ERP's own setting does the work
    "cellaflow": write_leased,
}


# --- the loop ------------------------------------------------------------


def run_job(order: Order, erp: Erp, jobs: JobStore, arm: str) -> str:
    """One email, start to finish. Returns what happened, for the transcript."""
    if jobs.is_done(order.job_id):
        return "skipped: already done"

    _die("before-write", order.po_no)

    before = len(erp.orders_for(order.po_no))

    try:
        result = ARMS[arm](order, erp)
    except ResponseLost:
        # The ambiguous case, and the job dies on it. A worker that gets a read
        # timeout has no way to find out whether the write landed, so it fails
        # the job and lets the supervisor start the work again -- which is the
        # same second attempt every crash scenario here makes, except that this
        # process exits through its error handlers rather than being killed.
        # For the leased arm that difference matters: an exception is how the
        # SDK is told the side effect did not happen, so it releases the lease
        # on the way out. A lost response is exactly the case where that
        # inference is wrong.
        print(f"[agent] response for {order.po_no} never arrived; failing the job", flush=True)
        raise
    except ErpError as exc:
        if exc.is_duplicate_po:
            # ERPNext refused the write because it already holds an order
            # against this PO. The order exists; this worker is not the one
            # that made it and is not told its name.
            print(f"[agent] ERP refused {order.po_no}: {exc.message[:120]}", flush=True)
            return "refused: duplicate purchase order"
        raise
    after = len(erp.orders_for(order.po_no))

    # Whether the arm performed the write or was handed a prior result.
    # Counted from the ERP rather than from a flag the arm sets, so an arm
    # cannot claim a hit for a call that just ran again.
    #
    # It errs in one direction only, and it is worth knowing which. When a
    # second worker is racing, the peer can create the order between `before`
    # and `after`, so a genuine cache hit here is recorded as "created". The
    # count can understate reuse and can never overstate it, which is the
    # right way round for a claim this benchmark makes about its own arm.
    reused = after == before

    _die("after-write", order.po_no)

    jobs.mark_done(order.job_id, order.email, result["name"])
    return f"{'reused' if reused else 'created'} {result['name']}"


def worker(orders, arm: str, jobs_path: pathlib.Path, session: str) -> None:
    """Process a list of orders in order. One pass of a worker."""
    erp = Erp()
    jobs = JobStore(jobs_path)

    if arm == "cellaflow":
        from cellaflow import durable_tools

        with durable_tools(session, target=ENGINE):
            for order in orders:
                print(f"[agent] {order.po_no}: {run_job(order, erp, jobs, arm)}", flush=True)
    else:
        for order in orders:
            print(f"[agent] {order.po_no}: {run_job(order, erp, jobs, arm)}", flush=True)

    jobs.close()
