"""Offline runtime: a deterministic stand-in for the model.

It runs the *real* hooks and the *real* tool handlers against the real database, using a
small rule-based planner in place of Claude. That makes the whole pipeline (policy,
idempotency, ledger verification, approvals, checkpoints, crash recovery) runnable and
testable without an API key or network.

It is not meant to be smart. It is meant to be predictable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from .contracts import CustomerReply, ResolutionSummary, TriageResult
from .hooks import STRUCTURED_OUTPUT_TOOL
from .policy import qualified
from .runtime import StageOutcome, StageSpec, validate_output

Override = Callable[[StageSpec[Any]], Awaitable[StageOutcome[Any]] | StageOutcome[Any]]

_ORDER = re.compile(r"\bORD-\d{4}\b", re.I)
_DOLLARS = re.compile(r"\$\s?(\d+)(?:\.(\d{2}))?")


class OfflineRuntime:
    def __init__(self, overrides: dict[str, Override] | None = None, cost_per_call: float = 0.001):
        self.overrides = overrides or {}
        self.cost_per_call = cost_per_call
        self.calls: list[str] = []

    async def run(self, spec: StageSpec[Any]) -> StageOutcome[Any]:
        self.calls.append(spec.name)
        if spec.name in self.overrides:
            result = self.overrides[spec.name](spec)
            return await result if hasattr(result, "__await__") else result  # type: ignore
        # Structured output goes through the hook too, exactly like the real runtime.
        handler = {"triage": self._triage, "resolve": self._resolve, "reply": self._reply}
        raw = await handler[spec.name](spec)
        await spec.policy.pre_tool_use(
            {"tool_name": STRUCTURED_OUTPUT_TOOL, "tool_input": raw}, None, None
        )  # type: ignore[arg-type]
        parsed, problem = validate_output(spec, raw)
        return StageOutcome(
            ok=problem is None,
            subtype="success",
            is_error=False,
            output=parsed,
            error=problem,
            cost_usd=self.cost_per_call,
            num_turns=1,
            duration_ms=1,
            session_id=f"offline-{spec.name}",
            model="offline",
            permission_denials=len(spec.policy.denials),
        )

    # ------------------------------------------------------------------ stages

    async def _triage(self, spec: StageSpec[TriageResult]) -> dict[str, Any]:
        text: str = spec.context["message"]
        low = text.lower()
        m = _ORDER.search(text)
        order_id = m.group(0).upper() if m else None
        intent = "other"
        if any(w in low for w in ("broken", "damaged", "cracked", "shattered")):
            intent = "damaged_item"
        elif "cancel" in low:
            intent = "cancel_order"
        elif "refund" in low or "money back" in low:
            intent = "refund_request"
        elif "address" in low:
            intent = "change_address"
        elif any(w in low for w in ("where", "status", "tracking", "arrive")):
            intent = "order_status"
        amount = None
        if d := _DOLLARS.search(text):
            amount = int(d.group(1)) * 100 + int(d.group(2) or 0)
        skus = re.findall(r"\b[A-Z]+-\d{2}\b", text)
        new_address = None
        if intent == "change_address" and " to " in text:
            new_address = text.split(" to ", 1)[1].strip().rstrip(".")
        return TriageResult(
            intent=intent,
            order_id=order_id,
            summary=text[:200],
            urgency="high" if intent == "damaged_item" else "normal",
            requested_refund_cents=amount,
            damaged_skus=skus,
            new_address=new_address,
        ).model_dump(mode="json")

    async def _tool(self, spec: StageSpec[Any], short: str, args: dict[str, Any]) -> dict:
        name = qualified(short)
        decision = await spec.policy.pre_tool_use(
            {"tool_name": name, "tool_input": args},
            f"tu-{short}",
            None,  # type: ignore[arg-type]
        )
        if decision.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
            return {"denied": True}
        impl = spec.tool_impls.get(name)
        if impl is None:
            return {"denied": True}
        result = await impl(args)
        await spec.policy.post_tool_use(
            {"tool_name": name, "tool_input": args, "tool_response": result},
            f"tu-{short}",
            None,  # type: ignore[arg-type]
        )
        text = result["content"][0]["text"]
        if result.get("is_error"):
            return {"error": text}
        return json.loads(text)

    async def _resolve(self, spec: StageSpec[ResolutionSummary]) -> dict[str, Any]:
        brief = spec.context["brief"]
        triage = brief["triage"]
        oid = brief["order_id"]
        tools = set(brief["available_tools"])
        order = await self._tool(spec, "get_order", {"order_id": oid})
        claimed: list[str] = []
        outcome = "no_action_needed"
        notes: list[str] = []

        def record(res: dict[str, Any], ok_action: str) -> None:
            nonlocal outcome
            if res.get("status") == "pending_approval":
                claimed.append("refund_pending_approval")
                outcome = "pending_approval"
            elif "error" not in res and not res.get("denied"):
                claimed.append(ok_action)
                outcome = "resolved"
            else:
                notes.append(res.get("error", "denied"))

        intent = triage["intent"]
        if intent == "order_status":
            ship = await self._tool(spec, "get_shipment", {"order_id": oid})
            notes.append(f"shipment: {ship.get('status')}")
        elif intent == "cancel_order" and "cancel_order" in tools:
            record(
                await self._tool(
                    spec, "cancel_order", {"order_id": oid, "reason": "customer request"}
                ),
                "order_cancelled",
            )
        elif intent == "change_address" and "update_shipping_address" in tools:
            record(
                await self._tool(
                    spec,
                    "update_shipping_address",
                    {"order_id": oid, "address": triage.get("new_address") or ""},
                ),
                "address_updated",
            )
        elif intent == "damaged_item" and "create_replacement" in tools:
            lines = {ln["sku"]: ln for ln in order.get("lines", [])}
            sku = next((s for s in triage["damaged_skus"] if s in lines), None) or next(iter(lines))
            inv = await self._tool(spec, "check_inventory", {"sku": sku})
            if inv.get("available", 0) >= 1:
                record(
                    await self._tool(
                        spec, "create_replacement", {"order_id": oid, "sku": sku, "qty": 1}
                    ),
                    "replacement_created",
                )
            else:
                amount = lines[sku]["unit_price_cents"]
                record(
                    await self._tool(
                        spec,
                        "issue_refund",
                        {
                            "order_id": oid,
                            "amount_cents": amount,
                            "reason": f"damaged {sku}, out of stock",
                        },
                    ),
                    "refund_issued",
                )
        elif intent == "refund_request" and "issue_refund" in tools:
            amount = triage.get("requested_refund_cents") or order.get("refundable_cents", 0)
            record(
                await self._tool(
                    spec,
                    "issue_refund",
                    {"order_id": oid, "amount_cents": amount, "reason": "customer refund request"},
                ),
                "refund_issued",
            )
        elif intent == "refund_request" and "cancel_order" in tools:
            record(
                await self._tool(
                    spec, "cancel_order", {"order_id": oid, "reason": "refund before shipping"}
                ),
                "order_cancelled",
            )
        else:
            await self._tool(
                spec, "escalate_to_human", {"reason": f"no automated path for intent {intent}"}
            )
            claimed.append("escalated")
            outcome = "escalated"

        return ResolutionSummary(
            outcome=outcome,
            actions_claimed=claimed,
            explanation="; ".join(notes) or "handled by offline planner",
        ).model_dump(mode="json")

    async def _reply(self, spec: StageSpec[CustomerReply]) -> dict[str, Any]:
        facts = spec.context["facts"]
        actions = facts["actions"]
        if facts["order_lookup"] != "found":
            body = (
                "We couldn't find that order on your account. Could you reply with the "
                "order number (it looks like ORD-1234)?"
            )
        elif not actions:
            o = facts["order"]
            body = f"Your order {o['order_id']} is currently {o['status']}."
        else:
            parts = []
            issued = any(a["action"] == "refund_issued" for a in actions)
            for a in actions:
                if a["action"] == "refund_pending_approval" and issued:
                    continue  # approved since; the issued row tells the story
                amt = f" of {a['amount']}" if a["amount"] else ""
                parts.append(
                    {
                        "refund_issued": f"we've issued a refund{amt}",
                        "refund_pending_approval": f"your refund{amt} is under review",
                        "order_cancelled": (
                            f"your order is cancelled and {a['amount']} will be returned"
                        ),
                        "replacement_created": "a replacement is on its way",
                        "address_updated": "your shipping address is updated",
                        "escalated": "a specialist will follow up shortly",
                    }[a["action"]]
                )
            body = "Good news: " + "; ".join(parts) + "."
        if any("declined" in n for n in facts["notes"]):
            body += (
                " After review we weren't able to approve the refund; a specialist will reach out."
            )
        return CustomerReply(
            subject=f"Update on ticket {facts['ticket_id']}",
            body=f"Hi,\n\n{body}\n\nOrder Support",
        ).model_dump(mode="json")
