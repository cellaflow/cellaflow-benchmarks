#!/usr/bin/env python3
"""Seeds the ERPNext site and writes the API credentials the agent uses.

Run by `setup.sh`, after the site exists and after ERPNext's own
`install_fixtures` has created the unit, group and territory records a fresh
site does not have. Everything this script adds -- the fiscal year, the
company, five customers, four items, one price list -- is the minimum a Sales
Order needs to validate.

It does not run ERPNext's setup wizard. The wizard would also install demo
data, holiday lists and letterheads, none of which this benchmark reads and
all of which would make the starting state harder to state exactly.

The API key and secret land in `.erp-credentials.json`, which is gitignored.
They are issued by the site itself, so they are worth nothing anywhere else,
and they still do not belong in a command line or a committed file.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
from typing import Any, Dict

import requests

HERE = pathlib.Path(__file__).parent
CREDENTIALS = HERE / ".erp-credentials.json"

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
COMPANY = "Benchmark Manufacturing"
ABBR = "BM"

CUSTOMERS = [
    "Northwind Fabrication",
    "Halvorsen Marine",
    "Crestline Dairy",
    "Tamworth Plastics",
    "Sligo Engineering",
]

ITEMS = [
    ("BRG-6204", "Deep groove ball bearing 6204"),
    ("SEAL-22", "Rotary shaft seal 22mm"),
    ("PMP-114", "Centrifugal pump 114"),
    ("VLV-80", "Butterfly valve DN80"),
]


class Admin:
    """An Administrator session. Only used for seeding; the agent uses a key."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.s = requests.Session()

    def login(self, tries: int = 90) -> None:
        for attempt in range(tries):
            try:
                r = self.s.post(
                    f"{self.base}/api/method/login",
                    json={"usr": "Administrator", "pwd": "admin"},
                    timeout=10,
                )
                if r.status_code == 200:
                    print(f"[seed] logged in after {attempt * 5}s")
                    return
            except requests.RequestException:
                pass
            if attempt % 6 == 0:
                print(f"[seed] waiting for the site ({attempt * 5}s)")
            time.sleep(5)
        raise SystemExit(
            f"[seed] {self.base} never answered a login. "
            "`docker compose -p erp-order-entry logs create-site` says why."
        )

    def call(self, method: str, path: str, **kw: Any) -> Dict[str, Any]:
        r = self.s.request(method, f"{self.base}{path}", timeout=180, **kw)
        if r.status_code >= 400:
            raise SystemExit(f"[seed] {method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json()

    def exists(self, doctype: str, name: str) -> bool:
        r = self.s.get(f"{self.base}/api/resource/{doctype}/{name}", timeout=60)
        return r.status_code == 200

    def ensure(self, doctype: str, name: str, doc: Dict[str, Any]) -> None:
        if self.exists(doctype, name):
            print(f"[seed] {doctype} {name} already there")
            return
        self.call("POST", f"/api/resource/{doctype}", json=doc)
        print(f"[seed] created {doctype} {name}")


def _usable_credentials(base: str):
    """The key already on file, if the site still accepts it.

    Tested rather than assumed: a key that survived a `docker compose down -v`
    names a site whose database is gone, and reusing it would fail every call
    later and further from the cause.
    """
    if not CREDENTIALS.exists():
        return None
    try:
        d = json.loads(CREDENTIALS.read_text())
        key, secret = d["api_key"], d["api_secret"]
    except (ValueError, KeyError):
        return None
    try:
        r = requests.get(
            f"{base.rstrip('/')}/api/resource/User/Administrator",
            headers={"Authorization": f"token {key}:{secret}"},
            timeout=30,
        )
    except requests.RequestException:
        return None
    return (key, secret) if r.status_code == 200 else None


def main() -> int:
    admin = Admin(BASE)
    admin.login()

    # A Sales Order dated 2026-11-04 needs a fiscal year that contains it, or
    # validation refuses the document before any of this benchmark's questions
    # become relevant.
    admin.ensure("Fiscal Year", "2026", {
        "doctype": "Fiscal Year",
        "year": "2026",
        "year_start_date": "2026-01-01",
        "year_end_date": "2026-12-31",
    })

    # `bench new-site --install-app erpnext` does not create the warehouse
    # types the setup wizard would, and company creation builds a Transit
    # warehouse that needs one. Nothing in this benchmark touches stock, but
    # the company will not insert without it.
    admin.ensure("Warehouse Type", "Transit",
                 {"doctype": "Warehouse Type", "name": "Transit"})

    # Creating a company builds its chart of accounts, which takes a while and
    # is why this script is slow the first time and instant afterwards.
    admin.ensure("Company", COMPANY, {
        "doctype": "Company",
        "company_name": COMPANY,
        "abbr": ABBR,
        "default_currency": "GBP",
        "country": "United Kingdom",
        "chart_of_accounts": "Standard",
    })

    # The setup wizard would create this. `install_fixtures` does not, and a
    # Sales Order needs a selling price list to validate against even when
    # every line carries an explicit rate.
    admin.ensure("Price List", "Standard Selling", {
        "doctype": "Price List",
        "price_list_name": "Standard Selling",
        "currency": "GBP",
        "selling": 1,
        "enabled": 1,
    })

    for customer in CUSTOMERS:
        admin.ensure("Customer", customer, {
            "doctype": "Customer",
            "customer_name": customer,
            "customer_type": "Company",
            # Leaf records, not the roots. ERPNext refuses a customer filed
            # under a group node.
            "customer_group": "Commercial",
            "territory": "United Kingdom",
        })

    for code, description in ITEMS:
        admin.ensure("Item", code, {
            "doctype": "Item",
            "item_code": code,
            "item_name": description,
            "item_group": "Products",
            "stock_uom": "Nos",
            "is_stock_item": 0,       # no warehouse, no stock ledger, no reservations
            "include_item_in_manufacturing": 0,
        })

    # Arms A, B and C need ERPNext to permit a second order against one PO, so
    # that what stops it is the agent rather than the ERP. Arm B2 turns this
    # back off and is the only row that measures the built-in refusal.
    admin.call("PUT", "/api/resource/Selling Settings/Selling Settings",
               json={"allow_against_multiple_purchase_orders": 1})
    print("[seed] Selling Settings: multiple orders against one customer PO allowed")

    # Generated once and reused. Calling `generate_keys` again replaces the
    # secret, and the old one lingers in the Redis user cache long enough that
    # the next few requests fail to decrypt it -- an authentication error that
    # looks like a bug in the agent and is not.
    # The guard the comment above describes, which was missing. Re-running
    # setup.sh is the obvious reaction to a half-finished stack, and without
    # this it rotated the secret every time: the next run then failed to
    # authenticate on every call, the control gate fired, and the transcript
    # blamed the agent.
    existing = _usable_credentials(BASE)
    if existing:
        key, secret = existing
        print("[seed] reusing the API key already on file")
    else:
        secret = admin.call(
            "POST", "/api/method/frappe.core.doctype.user.user.generate_keys",
            json={"user": "Administrator"},
        )["message"]["api_secret"]
        key = admin.call("GET", "/api/resource/User/Administrator")["data"]["api_key"]
        print("[seed] issued a new API key")

    CREDENTIALS.write_text(json.dumps(
        {"base_url": BASE, "api_key": key, "api_secret": secret}, indent=2) + "\n")
    CREDENTIALS.chmod(0o600)
    print(f"[seed] credentials written to {CREDENTIALS.name} (gitignored)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
