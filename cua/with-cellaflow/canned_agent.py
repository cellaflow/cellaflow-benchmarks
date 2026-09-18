#!/usr/bin/env python3
"""A deterministic stand-in for the model, registered through Cua's own decorator.

`@register_agent` is a public extension point in `cua_agent.decorators`, so this
is a supported way to drive the loop rather than a monkeypatch. The agent asks
for three clicks in order and then stops.

Replacing the model is what makes the run deterministic, free and reproducible.
It is the only thing replaced: the loop, the computer dispatch, the message
history and the turn structure are all Cua's.

The planner reads the conversation it is given before deciding, exactly as a
real model would. If Cua tells it an operation already happened, it skips that
operation. On the crash under test it is told nothing, which is the finding --
not an artefact of a stub ignoring what it was handed.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from cua_agent.decorators import register_agent

from checkout import OPERATIONS

_BUTTON_W, _BUTTON_H = 220, 60


def _centre(pos: Tuple[int, int]) -> Tuple[int, int]:
    return (pos[0] + _BUTTON_W // 2, pos[1] + _BUTTON_H // 2)


def completed_operations(messages: List[Dict[str, Any]]) -> set:
    """Which operations the conversation already shows as done.

    Cua carries prior turns forward in `messages`, including the computer_call
    items it executed. A model reading that history would not repeat them, so
    this reads it too. Anything found here is skipped.
    """
    done = set()
    blob = json.dumps(messages, default=str)
    for op, _label, pos in OPERATIONS:
        cx, cy = _centre(pos)
        # A prior computer_call whose coordinates land on this button.
        if re.search(rf'"x"\s*:\s*{cx}\b[^}}]*"y"\s*:\s*{cy}\b', blob) or \
           re.search(rf'"y"\s*:\s*{cy}\b[^}}]*"x"\s*:\s*{cx}\b', blob):
            done.add(op)
    return done


@register_agent(models=r"canned/checkout")
class CannedCheckoutAgent:
    """Clicks each operation once, in order, skipping any already recorded."""

    async def predict_step(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        max_retries: Optional[int] = None,
        stream: bool = False,
        computer_handler=None,
        _on_api_start=None,
        _on_api_end=None,
        _on_usage=None,
        _on_screenshot=None,
        **kwargs,
    ) -> Dict[str, Any]:
        done = completed_operations(messages)
        pending = [(op, pos) for op, _label, pos in OPERATIONS if op not in done]

        if not pending:
            return {
                "output": [{
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "checkout complete"}],
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }

        op, pos = pending[0]
        x, y = _centre(pos)
        return {
            "output": [{
                "type": "computer_call",
                "call_id": f"call_{op}",
                "status": "completed",
                "action": {"type": "click", "x": x, "y": y, "button": "left"},
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    async def predict_click(
        self, model: str, image_b64: str, instruction: str, **kwargs
    ) -> Optional[Tuple[float, float]]:
        for op, label, pos in OPERATIONS:
            if op in instruction.lower() or label.lower() in instruction.lower():
                return _centre(pos)
        return None

    def get_capabilities(self) -> List[str]:
        return ["step", "click"]
