#!/usr/bin/env python3
"""The same fake machine, with each irreversible operation leased.

Identical to ../checkout.py except that `click` routes the operation through a
CellaFlow `@tool` keyed on the business operation -- `charge:ORD-x` -- rather
than on the run, the process or the agent turn. The key is what a retry derives
again, so the second run recognises work the first one completed.
"""

from __future__ import annotations

import base64
import io
import json
import os
import pathlib
import time
from typing import Callable, Dict

from PIL import Image, ImageDraw

HERE = pathlib.Path(__file__).parent
LEDGER = HERE / "ledger.jsonl"
ENGINE = os.environ.get("CELLAFLOW_TARGET", "localhost:50051")
DIE_BETWEEN = os.environ.get("BENCH_DIE_BETWEEN") or None

OPERATIONS = [
    ("reserve", "Reserve stock", (100, 100)),
    ("charge", "Charge card", (100, 200)),
    ("confirm", "Send confirmation", (100, 300)),
]

_BUTTON_W, _BUTTON_H = 220, 60


def op_at(x: int, y: int) -> str | None:
    for op, _label, (bx, by) in OPERATIONS:
        if bx <= x <= bx + _BUTTON_W and by <= y <= by + _BUTTON_H:
            return op
    return None


def record(order_id: str, op: str) -> None:
    with LEDGER.open("a") as fh:
        fh.write(json.dumps({"order_id": order_id, "op": op, "at": time.time()}) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def operations_done(order_id: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    if not LEDGER.exists():
        return out
    for line in LEDGER.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec["order_id"] == order_id:
            out[rec["op"]] = out.get(rec["op"], 0) + 1
    return out


def _screen() -> str:
    img = Image.new("RGB", (640, 480), "white")
    draw = ImageDraw.Draw(img)
    draw.text((100, 40), "Checkout", fill="black")
    for _op, label, (bx, by) in OPERATIONS:
        draw.rectangle([bx, by, bx + _BUTTON_W, by + _BUTTON_H], outline="black", width=2)
        draw.text((bx + 12, by + 24), label, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def build_computer(order_id: str, die_after_op: str | None = None) -> Dict[str, Callable]:
    from cellaflow import tool

    def leased(op: str):
        @tool(idempotency_key=f"{op}:{order_id}")
        async def run_op(_marker: str) -> dict:
            record(order_id, op)
            if die_after_op == op:
                # Inside the tool, before it returns -- so the lease has not
                # committed. This is the unsurvivable window, and it is the
                # honest place to crash: the operation happened and nothing
                # anywhere recorded that it did.
                print(f"[cua] dying immediately after {op}", flush=True)
                os._exit(137)
            return {"performed": True}
        return run_op

    async def screenshot() -> str:
        return _screen()

    async def click(x: int, y: int, button: str = "left") -> None:
        op = op_at(x, y)
        if op is None:
            return
        # Count this operation specifically. An earlier version compared the
        # number of DISTINCT operations recorded, which does not change when an
        # operation repeats -- so it reported a cache hit for a call that had
        # just run again.
        before = operations_done(order_id).get(op, 0)
        await leased(op)(order_id)
        after = operations_done(order_id).get(op, 0)
        if after == before:
            print(f"[cua] lease returned a prior result for {op}; not repeating it", flush=True)
        if DIE_BETWEEN == op:
            # After the tool committed, before the next operation starts. The
            # survivable gap: the work is done and recorded, only the sequence
            # was interrupted.
            print(f"[cua] dying between {op} and the next operation", flush=True)
            os._exit(137)

    async def get_environment() -> str:
        return "linux"

    async def get_dimensions() -> tuple[int, int]:
        return (640, 480)

    async def type(text: str) -> None:  # noqa: A001
        return None

    async def wait(ms: int = 1000) -> None:
        return None

    return {
        "screenshot": screenshot,
        "click": click,
        "get_environment": get_environment,
        "get_dimensions": get_dimensions,
        "type": type,
        "wait": wait,
    }
