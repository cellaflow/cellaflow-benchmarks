#!/usr/bin/env python3
"""One worker process. Spawned by harness.py, and killed by it where the
scenario says so.

Every run is a real subprocess rather than a function call, so a crash is a
real process death and the next run really does start with nothing in memory.
"""

from __future__ import annotations

import os
import pathlib
import sys
import time

from agent import worker
from extract import inbox

ARM = os.environ["BENCH_ARM"]
JOBS_DB = pathlib.Path(os.environ["BENCH_JOBS_DB"])
SESSION = os.environ.get("BENCH_SESSION", "erp-order-entry")
# Two workers racing need to start together, or the second one arrives after
# the first has finished and there is no contention to measure.
START_AT = float(os.environ.get("BENCH_START_AT", "0"))


def main() -> int:
    JOBS_DB.parent.mkdir(parents=True, exist_ok=True)
    if START_AT:
        delay = START_AT - time.time()
        if delay > 0:
            time.sleep(delay)
    worker(inbox(), ARM, JOBS_DB, SESSION)
    print("[agent] pass finished", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
