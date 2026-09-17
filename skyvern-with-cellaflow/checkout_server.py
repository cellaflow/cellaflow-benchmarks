#!/usr/bin/env python3
"""A checkout page. The POST handlers are the irreversible effects.

Two pages. The default is one button posting to /place-order, which every
published scenario uses and which must not change. Under BENCH_PLAN=multi_op it
serves three buttons instead, one per separately-irreversible operation.

The three-button page posts with fetch() rather than a form. That is not
cosmetic: Skyvern resolves a batch's element ids from a single scrape taken
before the batch runs, so a navigation after the first click would leave the
remaining ids pointing at a DOM that no longer exists. Keeping the page still is
what makes a three-action batch possible at all -- and an AJAX checkout that
confirms out of band is a realistic shape in its own right.
"""
import json, os, pathlib, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

LEDGER = pathlib.Path(__file__).parent / "ledger.jsonl"

OPS = [("reserve", "Reserve stock"), ("charge", "Charge card"), ("confirm", "Send confirmation")]


def qs(path: str, key: str, default: str) -> str:
    return (parse_qs(urlparse(path).query).get(key) or [default])[0]


def single_page(order_id: str) -> bytes:
    return (
        "<!doctype html><html><body><h1>Checkout</h1>"
        f'<form method="POST" action="/place-order?order={order_id}">'
        '<button id="place-order" type="submit">Place order</button>'
        "</form></body></html>"
    ).encode()


def multi_page(order_id: str) -> bytes:
    buttons = "".join(
        f'<button id="{op}" onclick="go(\'{op}\')">{label}</button> '
        for op, label in OPS
    )
    return (
        "<!doctype html><html><body><h1>Checkout</h1>"
        "<script>function go(op){"
        f"fetch('/op?order={order_id}&op='+op,{{method:'POST'}});"
        "}</script>"
        f"{buttons}</body></html>"
    ).encode()


def record(order_id: str, op: str) -> None:
    with LEDGER.open("a") as fh:
        fh.write(json.dumps({"order_id": order_id, "op": op, "at": time.time()}) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        order_id = qs(self.path, "order", "ORD-unset")
        multi = os.environ.get("BENCH_PLAN") == "multi_op"
        self._send(multi_page(order_id) if multi else single_page(order_id))

    def do_POST(self):
        order_id = qs(self.path, "order", "ORD-unset")
        if urlparse(self.path).path == "/op":
            record(order_id, qs(self.path, "op", "unknown"))
            self._send(b"ok")
            return
        # The published single-button path. `op` is "place-order" so the six
        # existing scenarios keep a counter even though they never split.
        record(order_id, "place-order")
        if os.environ.get("BENCH_PAGE_MODE") == "silent":
            self._send(single_page(order_id))
        else:
            self._send(b"<html><body><h1>Order placed</h1></body></html>")


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
