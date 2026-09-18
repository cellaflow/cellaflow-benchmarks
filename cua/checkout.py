#!/usr/bin/env python3
"""The fake machine Cua drives, and the ledger that adjudicates.

Cua's agent talks to an `AsyncComputerHandler`. `CustomComputerHandler` accepts
a dict of callables and requires only `screenshot`, so the whole desktop can be
a few functions here — no VM, no Lume, no display server. That is Cua's own
documented extension point, not a patch.

Each click is an irreversible operation against an external system: it appends
to `ledger.jsonl` and fsyncs *before* returning, so a process that dies the
instant after acting is still counted as having acted. No arm can avoid a count
by dying before it reports one.
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

# Three separately-irreversible operations, and where they are on screen.
OPERATIONS = [
    ("reserve", "Reserve stock", (100, 100)),
    ("charge", "Charge card", (100, 200)),
    ("confirm", "Send confirmation", (100, 300)),
]

_BUTTON_W, _BUTTON_H = 220, 60


def op_at(x: int, y: int) -> str | None:
    """Which operation a click at (x, y) lands on."""
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
    """A base64 PNG of the checkout screen.

    Deliberately unchanging: the screen looks the same whether or not an
    operation has run. That is the realistic case for anything confirming out of
    band, and it isolates the question -- if the pixels do not say what already
    happened, does anything else?
    """
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
    """The function dict handed to `CustomComputerHandler`.

    `die_after_op` kills the process immediately after that operation has been
    recorded and before the agent loop regains control -- the window between an
    irreversible act and anything durable noting that the agent did it.
    """

    async def screenshot() -> str:
        return _screen()

    async def click(x: int, y: int, button: str = "left") -> None:
        op = op_at(x, y)
        if op is None:
            return
        record(order_id, op)
        if die_after_op == op:
            print(f"[cua] dying immediately after {op}", flush=True)
            os._exit(137)

    async def get_environment() -> str:
        return "linux"

    async def get_dimensions() -> tuple[int, int]:
        return (640, 480)

    async def type(text: str) -> None:  # noqa: A001 - name fixed by the protocol
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
