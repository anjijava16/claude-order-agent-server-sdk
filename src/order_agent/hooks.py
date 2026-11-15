"""Hooks: policy enforcement and audit, never control flow.

* PreToolUse   – allowlist, order pinning, mutation budget, refund pre-check. Denials carry
                 a reason the model can read and an audit record a human can read later.
* PostToolUse  – records every tool result (truncated) in the audit table.
* PostToolUseFailure – records failures separately, so a post-mortem can tell a policy
                 denial from a tool crash from a business-rule rejection.

Auditable permission decisions were the main gap reader feedback raised on Part 1.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from claude_agent_sdk import HookContext, HookMatcher

from .db import Store
from .policy import MUTATING_TOOLS, qualified

STRUCTURED_OUTPUT_TOOL = "StructuredOutput"
_MUTATING_QUALIFIED = {qualified(t) for t in MUTATING_TOOLS}


def deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _short(value: Any, limit: int = 2000) -> Any:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "…[truncated]"


class ToolPolicy:
    """Per-run hook set. One instance per stage run, so counters never leak across tickets."""

    def __init__(
        self,
        *,
        store: Store,
        ticket_id: str,
        stage: str,
        allowed: set[str],
        order_id: str | None = None,
        max_mutations: int = 2,
        refundable_cents: int | None = None,
    ) -> None:
        self.store = store
        self.ticket_id = ticket_id
        self.stage = stage
        self.allowed = set(allowed) | {STRUCTURED_OUTPUT_TOOL}
        self.order_id = order_id
        self.max_mutations = max_mutations
        self.refundable_cents = refundable_cents
        self.mutations_reserved = 0
        self.denials: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    def _audit(self, event: str, detail: dict[str, Any]) -> None:
        self.store.audit(self.ticket_id, event, {"stage": self.stage, **detail})

    async def _deny(self, tool_name: str, tool_input: dict[str, Any], reason: str) -> dict:
        record = {"tool": tool_name, "input": _short(tool_input), "reason": reason}
        self.denials.append(record)
        self._audit("tool_denied", record)
        return deny(reason)

    async def pre_tool_use(
        self, input_data: dict[str, Any], tool_use_id: str | None, context: HookContext
    ) -> dict[str, Any]:
        name = input_data.get("tool_name", "")
        args = input_data.get("tool_input") or {}

        if name not in self.allowed:
            return await self._deny(name, args, f"{name} is not available for this request")

        if name == STRUCTURED_OUTPUT_TOOL:
            return {}

        if self.order_id and "order_id" in args and args["order_id"] != self.order_id:
            return await self._deny(name, args, f"this ticket is pinned to order {self.order_id}")

        if name in _MUTATING_QUALIFIED:
            async with self._lock:  # parallel tool calls must not both pass the budget check
                if self.mutations_reserved >= self.max_mutations:
                    return await self._deny(
                        name, args, f"mutation budget of {self.max_mutations} actions is used up"
                    )
                if name == qualified("issue_refund") and self.refundable_cents is not None:
                    amount = int(args.get("amount_cents", 0) or 0)
                    if amount > self.refundable_cents:
                        return await self._deny(
                            name,
                            args,
                            f"refund {amount} exceeds refundable balance {self.refundable_cents}",
                        )
                self.mutations_reserved += 1

        self._audit("tool_requested", {"tool": name, "input": _short(args), "id": tool_use_id})
        return {}

    async def post_tool_use(
        self, input_data: dict[str, Any], tool_use_id: str | None, context: HookContext
    ) -> dict[str, Any]:
        self._audit(
            "tool_result",
            {
                "tool": input_data.get("tool_name"),
                "id": tool_use_id,
                "response": _short(input_data.get("tool_response")),
            },
        )
        return {}

    async def post_tool_use_failure(
        self, input_data: dict[str, Any], tool_use_id: str | None, context: HookContext
    ) -> dict[str, Any]:
        self._audit(
            "tool_failed",
            {
                "tool": input_data.get("tool_name"),
                "id": tool_use_id,
                "error": _short(input_data.get("error")),
            },
        )
        return {}

    def as_sdk_hooks(self) -> dict[str, list[HookMatcher]]:
        return {
            "PreToolUse": [HookMatcher(hooks=[self.pre_tool_use])],
            "PostToolUse": [HookMatcher(hooks=[self.post_tool_use])],
            "PostToolUseFailure": [HookMatcher(hooks=[self.post_tool_use_failure])],
        }
