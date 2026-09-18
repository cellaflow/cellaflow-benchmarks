#!/usr/bin/env python3
"""One Cua agent run against the fake checkout. Spawned per run by harness.py."""
from __future__ import annotations
import asyncio, os, sys

import canned_agent  # noqa: F401  -- registers the agent via @register_agent
from checkout import build_computer
from cua_agent import ComputerAgent
from cua_agent.computers.custom import CustomComputerHandler

ORDER_ID = os.environ["BENCH_ORDER_ID"]
DIE_AFTER = os.environ.get("BENCH_DIE_AFTER") or None


async def main() -> int:
    from cellaflow import durable_tools
    engine = os.environ.get("CELLAFLOW_TARGET", "localhost:50051")

    computer = CustomComputerHandler(build_computer(ORDER_ID, die_after_op=DIE_AFTER))
    agent = ComputerAgent(model="canned/checkout", tools=[computer], max_trajectory_budget=None)
    with durable_tools(f"cua:{ORDER_ID}", target=engine):
        async for _ in agent.run("Complete the checkout: reserve stock, charge the card, send confirmation."):
            pass
    print("[cua] run finished", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
