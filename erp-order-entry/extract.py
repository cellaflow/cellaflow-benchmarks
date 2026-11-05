#!/usr/bin/env python3
"""The model, faked deterministically.

A real deployment puts an LLM here: the email goes in, structured order fields
come out. The benchmark replaces it with a parser, for the same reason the
other harnesses in this repository replace the model -- a crash benchmark that
calls an API is neither free nor repeatable, and the model is not what is being
measured. What is being measured starts one line later, at the ERP write.

The substitution is only sound because extraction is *upstream* of every
scenario here. No scenario crashes during extraction, no arm guards it, and
every arm extracts identically. If a model produced slightly different line
items on a retry that would be a different and real failure, and it is measured
in ../multi-agent/ rather than here.
"""

from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass
from typing import List, Tuple

HERE = pathlib.Path(__file__).parent
FIXTURES = HERE / "fixtures"

# Appended to every purchase order number, so each run of the matrix works on
# orders the ERP and the engine have never seen. Real purchase order numbers
# are not reused; the only reason these would be is that the harness resets
# the ERP between runs, and a run tag undoes that artefact rather than
# introducing one. The agent never reads it: by the time an order reaches the
# agent the tag is simply part of the number the customer sent.
RUN_TAG = os.environ.get("BENCH_RUN_TAG", "")

# Sender domain to ERP customer name. An extraction model would infer this from
# the signature or the address book; here it is a dict, which is the same
# mapping with the variance removed.
CUSTOMER_BY_DOMAIN = {
    "northwind-fabrication.example": "Northwind Fabrication",
    "halvorsen-marine.example": "Halvorsen Marine",
    "crestline-dairy.example": "Crestline Dairy",
    "tamworth-plastics.example": "Tamworth Plastics",
    "sligo-engineering.example": "Sligo Engineering",
}

ITEM_CODES = ["BRG-6204", "SEAL-22", "PMP-114", "VLV-80"]

_PO = re.compile(r"\bPO-\d+\b")


@dataclass(frozen=True)
class Order:
    """What the extractor produces, and what the ERP write consumes."""

    email: str
    customer: str
    po_no: str
    items: Tuple[Tuple[str, int], ...]

    @property
    def job_id(self) -> str:
        """What identifies this unit of work.

        The customer's own PO number, not the filename, not a generated run id,
        and not a hash of the extracted fields. This choice is the whole
        benchmark: a key derived from the run cannot be re-derived by a retry,
        and a key derived from the extracted fields changes whenever the
        extraction does.
        """
        return self.po_no


def _quantities(body: str) -> List[Tuple[str, int]]:
    """Pull (item, qty) pairs out of a body, whichever way it is written.

    Three shapes appear in the fixtures and all three appear in real mail: a
    two-column table, "15 of BRG-6204" (sometimes "18 units of SEAL-22"), and
    "7 x PMP-114".
    """
    found: List[Tuple[str, int]] = []
    for code in ITEM_CODES:
        qty = None
        for pattern in (
            rf"{re.escape(code)}\s+(\d+)\b",          # table:   BRG-6204   40
            rf"(\d+)\s*(?:units?\s+of|x|of)\s*{re.escape(code)}",  # prose: 15 of BRG-6204
        ):
            m = re.search(pattern, body)
            if m:
                qty = int(m.group(1))
                break
        if qty:
            found.append((code, qty))
    # Sorted by the fixed code order above rather than by order of appearance,
    # so two emails listing the same items produce the same row order and a
    # diff between arms is never just a reordering.
    return found


def extract(path: pathlib.Path) -> Order:
    text = path.read_text()
    header, _, body = text.partition("\n\n")

    sender = re.search(r"^From:\s*(\S+)", header, re.M)
    if not sender:
        raise ValueError(f"{path.name}: no From header")
    domain = sender.group(1).split("@")[-1]
    customer = CUSTOMER_BY_DOMAIN.get(domain)
    if customer is None:
        raise ValueError(f"{path.name}: unknown sender domain {domain}")

    po = _PO.search(text)
    if not po:
        raise ValueError(f"{path.name}: no purchase order number")

    items = _quantities(body)
    if not items:
        raise ValueError(f"{path.name}: no line items")

    po_no = po.group(0) + (f"-{RUN_TAG}" if RUN_TAG else "")
    return Order(email=path.name, customer=customer, po_no=po_no, items=tuple(items))


def inbox() -> List[Order]:
    """The ten fixture emails, in a fixed order."""
    return [extract(p) for p in sorted(FIXTURES.glob("po-*.txt"))]


if __name__ == "__main__":
    for order in inbox():
        print(f"{order.email}  {order.po_no:<10} {order.customer:<24} {list(order.items)}")
