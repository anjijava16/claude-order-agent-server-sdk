"""Deterministic business policy.

The model never decides what it is *allowed* to do. Python decides, from the triaged
intent and the order's real state, which tools exist for this run. The resolution agent
then chooses among those tools. This is the "least privilege per stage" idea from the
article series applied to a transactional system.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import Order, OrderStatus, TriageResult

READ_TOOLS = ("get_order", "get_shipment", "check_inventory")
MUTATING_TOOLS = (
    "cancel_order",
    "issue_refund",
    "create_replacement",
    "update_shipping_address",
)
ALWAYS = ("escalate_to_human",)

SERVER_NAME = "orders"


def qualified(name: str) -> str:
    """SDK naming for in-process MCP tools: mcp__<server>__<tool>."""
    return f"mcp__{SERVER_NAME}__{name}"


@dataclass(frozen=True)
class Eligibility:
    tools: tuple[str, ...]
    notes: tuple[str, ...] = field(default_factory=tuple)
    blocked_reason: str | None = None

    @property
    def qualified_tools(self) -> list[str]:
        return [qualified(t) for t in self.tools]


def eligible_tools(triage: TriageResult, order: Order) -> Eligibility:
    notes: list[str] = []
    tools: list[str] = list(READ_TOOLS) + list(ALWAYS)
    status = order.status

    match triage.intent:
        case "order_status" | "other":
            pass
        case "cancel_order":
            if status in (OrderStatus.PLACED, OrderStatus.PAID):
                tools.append("cancel_order")
            else:
                notes.append(f"cancel not possible: order is {status.value}")
        case "change_address":
            if status in (OrderStatus.PLACED, OrderStatus.PAID, OrderStatus.PACKED):
                tools.append("update_shipping_address")
            else:
                notes.append(f"address change not possible: order is {status.value}")
        case "refund_request":
            if status == OrderStatus.CANCELLED:
                notes.append("order already cancelled and refunded")
            elif order.refundable_cents == 0:
                notes.append("nothing left to refund")
            elif status in (OrderStatus.PLACED, OrderStatus.PAID):
                # Not shipped yet: the right action is a cancellation, not a refund.
                tools.append("cancel_order")
            else:
                tools.append("issue_refund")
        case "damaged_item":
            if status in (OrderStatus.SHIPPED, OrderStatus.DELIVERED):
                tools.extend(["create_replacement", "issue_refund"])
            else:
                notes.append("item cannot be damaged in transit before it ships")

    return Eligibility(tools=tuple(dict.fromkeys(tools)), notes=tuple(notes))


def check_ownership(order: Order | None, customer_id: str) -> str | None:
    """Return a reason string when the customer may not act on this order."""
    if order is None:
        return "order_not_found"
    if order.customer_id != customer_id:
        # Same message as not-found so we don't leak that someone else's order exists.
        return "order_not_found"
    return None
