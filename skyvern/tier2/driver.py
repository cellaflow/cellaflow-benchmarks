#!/usr/bin/env python3
"""One Skyvern run against a local checkout page, with a canned planner.

Runs Skyvern's real agent loop -- real `execute_step`, real `retry_index`, real
Postgres writes, real Playwright -- and replaces exactly one thing: the LLM that
decides which element to click. That substitution is what makes the run
deterministic and free, and it is the only substitution made.

Invoked as a subprocess by harness.py so a crash is a real process death.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).parent
LEDGER = HERE / "ledger.jsonl"
PROMPT_DUMP = HERE / ".last_action_prompt.txt"

os.environ.setdefault("DATABASE_STRING", "postgresql+psycopg://skyvern:skyvern@localhost:5440/skyvern")
os.environ.setdefault("SKYVERN_STORAGE_TYPE", "local")
os.environ.setdefault("BROWSER_TYPE", "chromium-headless")
# Skyvern blocks navigation to loopback as an SSRF guard (webeye/navigation.py:61).
# The checkout page under test is local, so 127.0.0.1 is allow-listed through the
# hatch Skyvern already provides (utils/url_validators.py:_is_allowed_host).
# This is the ONLY configuration of Skyvern this harness changes, and it changes
# nothing about action execution, persistence or retry.
os.environ.setdefault("ALLOWED_HOSTS", '["127.0.0.1"]')
os.environ.setdefault("ENV", "local")

ORDER_ID = os.environ["BENCH_ORDER_ID"]
CHECKOUT_URL = os.environ["BENCH_CHECKOUT_URL"]
DIE_AT = os.environ.get("BENCH_DIE_AT", "no")     # no | after_click
ARM = os.environ.get("BENCH_ARM", "skyvern")      # skyvern | cellaflow
DUMP_PROMPT = os.environ.get("BENCH_DUMP_PROMPT") == "1"


# ---------------------------------------------------------------------------
# The canned planner
# ---------------------------------------------------------------------------

_calls = {"n": 0}


def _find_submit_element_id(prompt: str) -> str | None:
    """Pull the id of the 'Place order' control out of the scraped element tree.

    Skyvern renders clickable elements into the prompt as HTML with minted short
    ids, e.g. `<button type="submit" id="AAAC">Place order</button>`. The id is
    per-scrape, so it is matched on the button's label rather than hardcoded.
    """
    m = re.search(r'<[^>]*\bid="([^"]+)"[^>]*>\s*Place order\s*<', prompt, re.I)
    return m.group(1) if m else None


async def canned_llm_api_handler(prompt: str = "", prompt_name: str = "", **kwargs):
    _calls["n"] += 1

    if DUMP_PROMPT and "action" in prompt_name:
        PROMPT_DUMP.write_text(f"PROMPT_NAME={prompt_name}\n\n{prompt}")
        print(f"[driver] dumped prompt '{prompt_name}' ({len(prompt)} chars)", flush=True)

    if "action" not in prompt_name:
        # Verification / summary prompts: answer in the affirmative and move on.
        return {"page_info": "", "thoughts": "canned", "confident": True, "user_goal_achieved": True}

    element_id = _find_submit_element_id(prompt)
    if element_id is None:
        print("[driver] NO SUBMIT ELEMENT FOUND IN PROMPT", flush=True)
        return {"actions": [{"action_type": "COMPLETE", "reasoning": "canned: element not found",
                             "confidence_float": 1.0, "intention": "stop"}]}

    if _calls["n"] <= 1 or not _clicked["done"]:
        return {"actions": [{
            "action_type": "CLICK",
            "element_id": element_id,
            "reasoning": "canned planner: click the single button on the page",
            "confidence_float": 1.0,
            "intention": "place the order",
        }]}

    return {"actions": [{"action_type": "COMPLETE", "reasoning": "canned: order placed",
                         "confidence_float": 1.0, "intention": "finish"}]}


_clicked = {"done": False}


# ---------------------------------------------------------------------------
# Crash injection at the boundary the benchmark is about
# ---------------------------------------------------------------------------

def install_crash_after_click() -> None:
    """Kill the process the instant Skyvern's own `handle_action` returns.

    This wraps the boundary rather than editing Skyvern: the click has landed on
    the page, `handle_action` has produced its result, and nothing has persisted
    it yet. That is `agent.py:4288` -> `4299`, the window under test.
    """
    from skyvern.webeye.actions.handler import ActionHandler

    original = ActionHandler.handle_action

    async def wrapped(*args, **kwargs):
        result = await original(*args, **kwargs)
        _clicked["done"] = True
        if DIE_AT == "after_click":
            print("[driver] dying after handle_action, before any persist", flush=True)
            sys.stdout.flush()
            os._exit(137)
        return result

    ActionHandler.handle_action = wrapped  # type: ignore[method-assign]


# ---------------------------------------------------------------------------

async def main() -> int:
    from skyvern.forge.forge_app_initializer import start_forge_app

    start_forge_app()
    from skyvern.forge import app
    from skyvern.forge.sdk.schemas.tasks import TaskStatus

    app.LLM_API_HANDLER = canned_llm_api_handler
    for attr in ("SECONDARY_LLM_API_HANDLER", "EXTRACTION_LLM_API_HANDLER",
                 "SELECT_AGENT_LLM_API_HANDLER", "NORMAL_SELECT_AGENT_LLM_API_HANDLER",
                 "CHECK_USER_GOAL_LLM_API_HANDLER"):
        if hasattr(app, attr):
            setattr(app, attr, canned_llm_api_handler)

    install_crash_after_click()

    org = await app.DATABASE.organizations.create_organization(
        organization_name=f"bench-{ORDER_ID}",
        max_steps_per_run=3,
        max_retries_per_step=1,
    )

    reuse_task_id = os.environ.get("BENCH_TASK_ID") or None
    reuse_org_id = os.environ.get("BENCH_ORG_ID") or None

    if reuse_task_id:
        # Skyvern's own retry shape: same task, a new step at retry_index + 1
        # (agent.py:8587 gates on step.retry_index >= max_retries_per_step).
        org = await app.DATABASE.organizations.get_organization(organization_id=reuse_org_id)
        task = await app.DATABASE.tasks.get_task(reuse_task_id, organization_id=reuse_org_id)
        prior = await app.DATABASE.tasks.get_latest_step(reuse_task_id, organization_id=reuse_org_id)
        step = await app.DATABASE.tasks.create_step(
            task_id=task.task_id,
            order=prior.order if prior else 0,
            retry_index=(prior.retry_index + 1) if prior else 0,
            organization_id=reuse_org_id,
        )
        print(f"[driver] RETRY org={reuse_org_id} task={task.task_id} step={step.step_id} "
              f"retry_index={step.retry_index}", flush=True)
        await _execute(app, org, task, step)
        return 0

    task = await app.DATABASE.tasks.create_task(
        url=CHECKOUT_URL,
        title=f"order {ORDER_ID}",
        navigation_goal="Click the Place order button exactly once.",
        data_extraction_goal=None,
        navigation_payload=None,
        organization_id=org.organization_id,
        status=TaskStatus.running,
    )

    step = await app.DATABASE.tasks.create_step(
        task_id=task.task_id,
        order=0,
        retry_index=0,
        organization_id=org.organization_id,
    )

    print(f"[driver] IDS org={org.organization_id} task={task.task_id} step={step.step_id}", flush=True)

    await _execute(app, org, task, step)
    return 0


async def _execute(app, org, task, step) -> None:
    ORDER_ID_LOCAL = ORDER_ID
    # execute_step calls skyvern_context.ensure_context(), which raises unless a
    # context has been set. The server normally does this per request.
    from skyvern.forge.sdk.core import skyvern_context
    from skyvern.forge.sdk.core.skyvern_context import SkyvernContext

    skyvern_context.set(
        SkyvernContext(
            organization_id=org.organization_id,
            task_id=task.task_id,
            request_id=f"bench-{ORDER_ID}",
            max_steps_override=3,
        )
    )

    from skyvern.forge.agent import ForgeAgent

    agent = ForgeAgent()
    await agent.execute_step(organization=org, task=task, step=step)
    print("[driver] execute_step returned", flush=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
