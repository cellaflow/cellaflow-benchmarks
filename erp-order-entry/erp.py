#!/usr/bin/env python3
"""Talks to ERPNext over its REST API, the way an agent would.

Nothing here is a stand-in. The orders are real `Sales Order` documents in a
real ERPNext, created and submitted through the same `/api/resource` endpoints
an integration would use, with an API key and secret issued by the site. The
count at the end of each run is a query against the ERP, not against anything
this harness wrote down.

That is deliberate and it is stronger than the fsynced-ledger rule the other
benchmarks in this repository follow. A ledger is needed when the external
service is faked, to stop a process avoiding a count by dying the instant after
acting. Here the external service is real and the record is its own: MariaDB
has committed the row before the HTTP response is written, so a client that
dies on the next instruction has still, unarguably, created the order.
"""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

HERE = pathlib.Path(__file__).parent
CREDENTIALS = HERE / ".erp-credentials.json"

COMPANY = "Benchmark Manufacturing"
ABBR = "BM"

# Every scenario's fault is injected here rather than scattered through the
# agent, so the agent code is the same in all three arms.
FAULT = os.environ.get("BENCH_FAULT", "")
FAULT_ON = os.environ.get("BENCH_FAULT_ON", "")

# `lose-response` fires once and disarms. A fault that fired on every attempt
# would be a dead ERP, which is a different and much easier problem: the worker
# never gets an answer, gives up, and a human looks at it. The hard case is the
# one that happens once, looks like a failure, and was not one.
_fault_spent = False


class ResponseLost(Exception):
    """The ERP committed the order and the answer never arrived.

    Raised in place of returning, so the caller is in exactly the position a
    client is in after a read timeout: the write may have landed, may not have,
    and there is no way to tell from here.
    """


@dataclass
class Credentials:
    base_url: str
    api_key: str
    api_secret: str

    @classmethod
    def load(cls) -> "Credentials":
        if not CREDENTIALS.exists():
            raise SystemExit(
                f"No {CREDENTIALS.name}. Run ./setup.sh first -- it creates the site, "
                "seeds the company and writes the API credentials."
            )
        d = json.loads(CREDENTIALS.read_text())
        return cls(
            base_url=os.environ.get("ERP_URL", d["base_url"]),
            api_key=d["api_key"],
            api_secret=d["api_secret"],
        )


class Erp:
    def __init__(self, creds: Optional[Credentials] = None, timeout: int = 60) -> None:
        self.creds = creds or Credentials.load()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["Authorization"] = (
            f"token {self.creds.api_key}:{self.creds.api_secret}"
        )
        self.session.headers["Accept"] = "application/json"

    # -- plumbing ---------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.creds.base_url.rstrip('/')}{path}"

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        r = self.session.request(method, self._url(path), timeout=self.timeout, **kw)
        if r.status_code >= 400:
            raise ErpError(r.status_code, _frappe_message(r))
        return r.json()

    # -- reads ------------------------------------------------------------

    def orders_for(self, po_no: str) -> List[Dict[str, Any]]:
        """Every Sales Order carrying this customer PO number.

        `docstatus < 2` excludes cancelled documents and includes both drafts
        and submitted orders, because a duplicate draft sitting in the ERP is
        still a duplicate somebody has to deal with.
        """
        out = self._request(
            "GET",
            "/api/resource/Sales Order",
            params={
                "filters": json.dumps([["po_no", "=", po_no], ["docstatus", "<", 2]]),
                "fields": json.dumps(["name", "po_no", "customer", "grand_total", "docstatus"]),
                "limit_page_length": 0,
            },
        )
        return out.get("data", [])

    def all_orders(self) -> List[Dict[str, Any]]:
        out = self._request(
            "GET",
            "/api/resource/Sales Order",
            params={
                "fields": json.dumps(["name", "po_no", "customer", "docstatus"]),
                "limit_page_length": 0,
            },
        )
        return out.get("data", [])

    # -- the write under test --------------------------------------------

    def create_sales_order(self, order: Any) -> Dict[str, Any]:
        """Create and submit one Sales Order. The irreversible operation.

        A submitted Sales Order is a commitment: it reserves nothing by itself
        but it is what downstream delivery, invoicing and revenue recognition
        read. Two of them against one customer PO is the failure this benchmark
        counts.
        """
        doc = {
            "doctype": "Sales Order",
            "company": COMPANY,
            "customer": order.customer,
            "po_no": order.po_no,
            "transaction_date": "2026-11-04",
            "delivery_date": "2026-11-18",
            "currency": "GBP",
            "selling_price_list": "Standard Selling",
            "items": [
                {
                    "item_code": code,
                    "qty": qty,
                    "rate": 10,
                    "delivery_date": "2026-11-18",
                }
                for code, qty in order.items
            ],
        }
        created = self._request("POST", "/api/resource/Sales Order", json=doc)["data"]
        name = created["name"]

        # ERPNext inserts as a draft. Submitting is what makes it an order
        # rather than a note, so the operation is not finished until this
        # returns. Both calls are inside the guarded region in every arm.
        self._request(
            "PUT", f"/api/resource/Sales Order/{name}", json={"docstatus": 1}
        )

        global _fault_spent
        if FAULT == "lose-response" and not _fault_spent and order.po_no == FAULT_ON:
            # The ERP has committed. The client never finds out.
            _fault_spent = True
            raise ResponseLost(f"response for {order.po_no} dropped after commit")

        return {"name": name, "po_no": order.po_no, "customer": order.customer}

    # -- reset between runs ----------------------------------------------

    def reset(self) -> int:
        """Remove every Sales Order, so each run starts from the same ERP.

        Submitted documents have to be cancelled before they can be deleted,
        which is two calls per order. Everything else the site holds -- the
        company, the customers, the items, the price list -- is seeded once and
        left alone.
        """
        removed = 0
        for so in self.all_orders():
            name = so["name"]
            if so["docstatus"] == 1:
                try:
                    self._request(
                        "PUT", f"/api/resource/Sales Order/{name}", json={"docstatus": 2}
                    )
                except ErpError:
                    pass
            self._request("DELETE", f"/api/resource/Sales Order/{name}")
            removed += 1
        return removed

    # -- the built-in guard, arm B2 --------------------------------------

    def set_allow_multiple_po(self, allowed: bool) -> None:
        """Flip `Selling Settings.allow_against_multiple_purchase_orders`.

        ERPNext ships this unchecked, meaning it refuses a second Sales Order
        against a customer PO number it has already seen. Arms A, B and C turn
        it on so the ERP is not doing the agent's job for it; arm B2 turns it
        back off and measures what it does.
        """
        self._request(
            "PUT",
            "/api/resource/Selling Settings/Selling Settings",
            json={"allow_against_multiple_purchase_orders": 1 if allowed else 0},
        )


class ErpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message

    @property
    def is_duplicate_po(self) -> bool:
        """Whether this is ERPNext refusing a second order against one PO."""
        return "already exists against Customer's Purchase Order" in self.message


def _frappe_message(r: requests.Response) -> str:
    """Dig the human-readable error out of a Frappe response.

    Frappe returns validation failures as a JSON envelope whose useful part is
    a JSON-encoded list inside `_server_messages`, so getting at it takes two
    decodes. Without this the table would report `HTTP 417` for everything and
    the duplicate-PO refusal would be indistinguishable from a bad item code.
    """
    try:
        body = r.json()
    except ValueError:
        return r.text[:300]
    msgs = body.get("_server_messages")
    if msgs:
        try:
            parsed = json.loads(msgs)
            out = []
            for m in parsed:
                try:
                    out.append(json.loads(m).get("message", m))
                except (ValueError, AttributeError):
                    out.append(str(m))
            return " | ".join(out)
        except ValueError:
            pass
    return body.get("exception") or body.get("message") or r.text[:300]
