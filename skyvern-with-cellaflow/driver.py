#!/usr/bin/env python3
"""One Skyvern run against a local checkout page, WITH the CellaFlow integration.

Identical to the audit's driver except for two blocks, both marked CELLAFLOW
below: an execution lease around the run, and the checkout click routed through
a leased tool. Everything else -- the canned planner, the crash injection, the
Skyvern setup -- is byte-for-byte the same, so the difference in the table is
the integration and nothing else.

Original:

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
PLAN = os.environ.get("BENCH_PLAN", "single")   # single | batch_then_fail | multi_op
PLANNER = os.environ.get("BENCH_PLANNER", "memo")  # memo | stateless


# ---------------------------------------------------------------------------
# The canned planner
# ---------------------------------------------------------------------------

_calls = {"n": 0}


MULTI_OPS = [("reserve", "Reserve stock"), ("charge", "Charge card"), ("confirm", "Send confirmation")]
_ELEMENTS: dict = {}


def _find_element_id(prompt: str, label: str) -> str | None:
    """Resolve one button's id from the scrape Skyvern renders into the prompt.

    Ids are minted per scrape (`<button id="AAAC">Place order</button>`), so they
    are matched on the visible label rather than hardcoded.
    """
    m = re.search(rf'<[^>]*\bid="([^"]+)"[^>]*>\s*{re.escape(label)}\s*<', prompt, re.I)
    if m:
        _ELEMENTS[label] = m.group(1)
    return m.group(1) if m else None


def _completed_ops(prompt: str) -> set:
    """Which operations Skyvern's own action history says already happened.

    A real model is told this and would not repeat them. Reading it here is what
    keeps the result about Skyvern rather than about a stub that ignores what it
    was given -- and on a mid-step crash this comes back empty, because the
    history is built from step outputs that were never written.
    """
    m = re.search(r"Action history from previous steps:\s*(\[.*?\])\s*\n", prompt, re.S)
    if not m:
        return set()
    done = set()
    for op, label in MULTI_OPS:
        eid = _ELEMENTS.get(label)
        if eid and f'"element_id": "{eid}"' in m.group(1):
            done.add(op)
    return done


def _click(element_id: str, why: str) -> dict:
    return {
        "action_type": "CLICK",
        "element_id": element_id,
        "reasoning": f"canned planner: {why}",
        "confidence_float": 1.0,
        "intention": why,
    }


def _find_submit_element_id(prompt: str):
    return _find_element_id(prompt, "Place order")


async def canned_llm_api_handler(prompt: str = "", prompt_name: str = "", **kwargs):
    _calls["n"] += 1

    if DUMP_PROMPT and "action" in prompt_name:
        (HERE / f".prompt_call{_calls['n']}.txt").write_text(f"PROMPT_NAME={prompt_name}\n\n{prompt}")
        print(f"[driver] dumped prompt '{prompt_name}' ({len(prompt)} chars)", flush=True)

    if "action" not in prompt_name:
        # Verification / summary prompts: answer in the affirmative and move on.
        return {"page_info": "", "thoughts": "canned", "confident": True, "user_goal_achieved": True}

    if PLAN == "multi_op":
        # One batch, three separately-irreversible operations. A batch rather
        # than three steps deliberately: step.output is written when a step
        # ends, so three steps would leave a populated history and the retry
        # would correctly skip. Only a crash inside one batch loses it.
        for _op, _label in MULTI_OPS:
            _find_element_id(prompt, _label)
        done = _completed_ops(prompt)
        pending = [(op, lbl) for op, lbl in MULTI_OPS if op not in done and _ELEMENTS.get(lbl)]
        if not pending:
            return {"actions": [{"action_type": "COMPLETE", "reasoning": "canned: all operations recorded",
                                 "confidence_float": 1.0, "intention": "finish"}]}
        return {"actions": [_click(_ELEMENTS[lbl], f"perform {op}") for op, lbl in pending]}

    element_id = _find_submit_element_id(prompt)
    if element_id is None:
        print("[driver] NO SUBMIT ELEMENT FOUND IN PROMPT", flush=True)
        return {"actions": [{"action_type": "COMPLETE", "reasoning": "canned: element not found",
                             "confidence_float": 1.0, "intention": "stop"}]}

    click = {
        "action_type": "CLICK",
        "element_id": element_id,
        "reasoning": "canned planner: click the single button on the page",
        "confidence_float": 1.0,
        "intention": "place the order",
    }

    if PLAN == "batch_then_fail" and not _clicked["done"]:
        # A batch whose LAST action fails after the order already went through.
        # This is the duplicate path that needs no crash: the step fails, Skyvern
        # retries the whole step, and the only thing standing between that and a
        # second order is whether the re-plan notices the first one.
        return {"actions": [click, {
            "action_type": "CLICK",
            "element_id": "ZZZZ-does-not-exist",
            "reasoning": "canned planner: deliberate failure to force a step retry",
            "confidence_float": 1.0,
            "intention": "force retry",
        }]}

    if PLANNER == "stateless":
        # A real LLM has no memory between calls -- it knows only what the prompt
        # tells it. If Skyvern's prompt carries the action history, a model can
        # tell it already clicked; if it does not, the only signal is the page.
        # Clicking whenever the button is visible is what a model with no other
        # information would do, so this isolates what Skyvern actually supplies.
        return {"actions": [click]}

    if _calls["n"] <= 1 or not _clicked["done"]:
        return {"actions": [click]}

    return {"actions": [{"action_type": "COMPLETE", "reasoning": "canned: order placed",
                         "confidence_float": 1.0, "intention": "finish"}]}


_clicked = {"done": False}


# ---------------------------------------------------------------------------
# Crash injection at the boundary the benchmark is about
# ---------------------------------------------------------------------------

def install_leased_action() -> None:
    """CELLAFLOW: route each irreversible operation through its own lease.

    The key is the business operation -- `reserve:ORD-x`, `charge:ORD-x` -- not
    the task, the step or the run. A retry under a new step id derives the same
    key and takes a hit, which is the whole mechanism.

    On a hit the operation is not repeated and the action is reported to Skyvern
    as having succeeded, which is true: it succeeded in the run that died.
    """
    from cellaflow import tool
    from skyvern.webeye.actions.handler import ActionHandler
    from skyvern.webeye.actions.responses import ActionSuccess

    original = ActionHandler.handle_action

    def _leased(op: str):
        @tool(idempotency_key=f"{op}:{ORDER_ID}")
        async def run_op(_marker: str) -> dict:
            results = await original(**_pending["kwargs"])
            _clicked["done"] = True
            if DIE_AT == "after_click":
                print("[driver] dying after the action, before the lease commits", flush=True)
                os._exit(137)
            return {"performed": True,
                    "ok": all(getattr(r, "success", False) for r in results)}
        return run_op

    def _op_for(element_id: str) -> str | None:
        for op, label in MULTI_OPS:
            if _ELEMENTS.get(label) == element_id:
                return op
        return None

    async def wrapped(*args, **kwargs):
        action = kwargs.get("action")
        element_id = getattr(action, "element_id", None) if action is not None else None
        op = _op_for(element_id) if element_id else None
        if op is None and element_id != _ELEMENTS.get("Place order"):
            return await original(*args, **kwargs)
        op = op or "place_order"

        if DIE_AT == "before_click":
            print("[driver] dying BEFORE handle_action runs", flush=True)
            os._exit(137)

        _pending["kwargs"] = kwargs
        before = _clicked["done"]
        outcome = await _leased(op)(ORDER_ID)
        if outcome.get("performed") and not _clicked["done"] and not before:
            print(f"[driver] lease returned a prior result for {op}; not repeating it", flush=True)
        _clicked["done"] = before

        _ops_done["n"] += 1
        if DIE_AT == "between_ops" and _ops_done["n"] == 2:
            # Two operations have landed and BOTH have committed through @tool.
            # The kill must be here and not inside the tool body: a kill before
            # the commit leaves no cached result, the retry re-runs, and the row
            # measures nothing.
            print("[driver] dying between operation 2 and operation 3", flush=True)
            sys.stdout.flush()
            os._exit(137)
        return [ActionSuccess()]

    ActionHandler.handle_action = wrapped  # type: ignore[method-assign]


_pending: dict = {}
_ops_done = {"n": 0}   # completed operations in this process


def install_crash_after_click() -> None:
    """Kill the process the instant Skyvern's own `handle_action` returns.

    This wraps the boundary rather than editing Skyvern: the click has landed on
    the page, `handle_action` has produced its result, and nothing has persisted
    it yet. That is `agent.py:4288` -> `4299`, the window under test.
    """
    from skyvern.webeye.actions.handler import ActionHandler

    original = ActionHandler.handle_action

    async def wrapped(*args, **kwargs):
        if DIE_AT == "before_click":
            print("[driver] dying BEFORE handle_action runs", flush=True)
            os._exit(137)
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

    install_leased_action()

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

    if os.environ.get("BENCH_SETUP_ONLY") == "1":
        # Leave a running task with a `created` step and execute nothing, so two
        # processes can be raced against the same unclaimed position.
        return 0

    await _execute(app, org, task, step)
    return 0


async def _execute(app, org, task, step) -> None:
    """CELLAFLOW: hold an execution lease over the task for the whole run.

    Holding it means any `running` step on this task belongs to a holder that
    stopped heartbeating. That is what licenses clearing them -- the question
    Skyvern's status column cannot answer.
    """
    from integration import LeaseNotAcquired, clear_stale_running_steps, task_execution_lease
    from cellaflow import durable_tools

    worker = f"worker-{os.getpid()}"
    try:
        with task_execution_lease(task.task_id, worker):
            cleared = await clear_stale_running_steps(app, task.task_id, org.organization_id)
            if cleared:
                print(f"[driver] cleared {cleared} stale running step(s)", flush=True)
            with durable_tools(f"skyvern:{ORDER_ID}", target=os.environ.get("CELLAFLOW_TARGET", "localhost:50051")):
                await _execute_inner(app, org, task, step)
    except LeaseNotAcquired as e:
        print(f"[driver] refused: {e}", flush=True)
    return


async def _execute_inner(app, org, task, step) -> None:
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
    # execute_step returns (step, output, next_step). Skyvern's server drives the
    # chain; a bare single call would stop before any retry, so the loop here is
    # what makes its own retry path actually run.
    hops = 0
    while step is not None and hops < 8:
        hops += 1
        _, _, next_step = await agent.execute_step(organization=org, task=task, step=step)
        print(f"[driver] hop={hops} step={step.step_id} retry_index={step.retry_index} "
              f"-> next={(next_step.step_id + ' retry_index=' + str(next_step.retry_index)) if next_step else None}",
              flush=True)
        step = next_step
    print("[driver] chain finished", flush=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
