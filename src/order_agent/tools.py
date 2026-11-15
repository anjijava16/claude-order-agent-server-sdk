"""In-process MCP tools for the resolution agent.

Tools are created per ticket and closed over that ticket's context (ticket id, customer,
order). The model never passes a customer id, so it can't act on someone else's order
even if the customer message contains instructions to try.

Business-rule failures come back as tool errors (`is_error=True`) with a readable reason,
so the agent can correct itself within the same run (e.g. offer a refund when a
replacement is out of stock) instead of the whole run failing.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server, tool
from mcp.types import ToolAnnotations

from .config import Settings
from .db import DomainError, Store
from .policy import SERVER_NAME

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}


def err(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"ERROR: {message}"}], "is_error": True}


READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
MUTATES = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)


@dataclass
class ToolContext:
    store: Store
    settings: Settings
    ticket_id: str
    customer_id: str
    order_id: str


def _guard_order(ctx: ToolContext, args: dict[str, Any]) -> str | None:
    requested = str(args.get("order_id", "")).strip()
    if requested != ctx.order_id:
        return f"this ticket may only act on order {ctx.order_id}"
    return None


def build_tools(ctx: ToolContext) -> dict[str, SdkMcpTool[Any]]:
    """Return every tool keyed by short name. Callers expose only the eligible subset."""

    @tool(
        "get_order",
        "Fetch the order: status, lines, totals, refundable balance, shipping address.",
        {"order_id": Annotated[str, "Order id, e.g. ORD-1001"]},
        annotations=READ_ONLY,
    )
    async def get_order(args: dict[str, Any]) -> dict[str, Any]:
        if (problem := _guard_order(ctx, args)) is not None:
            return err(problem)
        order = ctx.store.get_order(ctx.order_id)
        if order is None:
            return err("order not found")
        data = order.model_dump(mode="json")
        data["refundable_cents"] = order.refundable_cents
        return ok(data)

    @tool(
        "get_shipment",
        "Fetch carrier, tracking number, shipment status and ETA for the order.",
        {"order_id": str},
        annotations=READ_ONLY,
    )
    async def get_shipment(args: dict[str, Any]) -> dict[str, Any]:
        if (problem := _guard_order(ctx, args)) is not None:
            return err(problem)
        shipment = ctx.store.get_shipment(ctx.order_id)
        return ok(shipment or {"order_id": ctx.order_id, "status": "not_shipped"})

    @tool(
        "check_inventory",
        "Check available stock for a SKU before offering a replacement.",
        {"sku": str},
        annotations=READ_ONLY,
    )
    async def check_inventory(args: dict[str, Any]) -> dict[str, Any]:
        inv = ctx.store.get_inventory(str(args["sku"]))
        return ok(inv) if inv else err(f"unknown sku {args['sku']}")

    @tool(
        "cancel_order",
        "Cancel an order that has not shipped. Refunds the full remaining balance.",
        {"order_id": str, "reason": Annotated[str, "Short reason for the audit log"]},
        annotations=MUTATES,
    )
    async def cancel_order(args: dict[str, Any]) -> dict[str, Any]:
        if (problem := _guard_order(ctx, args)) is not None:
            return err(problem)
        try:
            return ok(
                ctx.store.cancel_order(
                    ticket_id=ctx.ticket_id, order_id=ctx.order_id, reason=args["reason"]
                )
            )
        except DomainError as e:
            return err(str(e))

    @tool(
        "issue_refund",
        "Refund part or all of the order in cents. Refunds above the auto-approval limit "
        "are parked for a human and return status=pending_approval. Call at most once.",
        {
            "order_id": str,
            "amount_cents": Annotated[int, "Positive integer, cents"],
            "reason": str,
        },
        annotations=MUTATES,
    )
    async def issue_refund(args: dict[str, Any]) -> dict[str, Any]:
        if (problem := _guard_order(ctx, args)) is not None:
            return err(problem)
        try:
            return ok(
                ctx.store.create_refund(
                    ticket_id=ctx.ticket_id,
                    order_id=ctx.order_id,
                    amount_cents=int(args["amount_cents"]),
                    reason=str(args["reason"]),
                    auto_limit_cents=ctx.settings.auto_refund_limit_cents,
                )
            )
        except DomainError as e:
            return err(str(e))

    @tool(
        "create_replacement",
        "Ship a replacement for a damaged item on this order. Fails if out of stock.",
        {"order_id": str, "sku": str, "qty": int},
        annotations=MUTATES,
    )
    async def create_replacement(args: dict[str, Any]) -> dict[str, Any]:
        if (problem := _guard_order(ctx, args)) is not None:
            return err(problem)
        try:
            return ok(
                ctx.store.create_replacement(
                    ticket_id=ctx.ticket_id,
                    order_id=ctx.order_id,
                    sku=str(args["sku"]),
                    qty=int(args["qty"]),
                )
            )
        except DomainError as e:
            return err(str(e))

    @tool(
        "update_shipping_address",
        "Change the shipping address of an order that has not shipped.",
        {"order_id": str, "address": str},
        annotations=MUTATES,
    )
    async def update_shipping_address(args: dict[str, Any]) -> dict[str, Any]:
        if (problem := _guard_order(ctx, args)) is not None:
            return err(problem)
        address = str(args["address"]).strip()
        if len(address) < 10:
            return err("address looks incomplete; ask the customer for the full address")
        try:
            return ok(
                ctx.store.update_address(
                    ticket_id=ctx.ticket_id, order_id=ctx.order_id, address=address
                )
            )
        except DomainError as e:
            return err(str(e))

    @tool(
        "escalate_to_human",
        "Hand the ticket to a human agent when policy doesn't cover the request or the "
        "customer's intent is unclear.",
        {"reason": str},
        annotations=MUTATES,
    )
    async def escalate_to_human(args: dict[str, Any]) -> dict[str, Any]:
        return ok(
            ctx.store.record_escalation(
                ticket_id=ctx.ticket_id, order_id=ctx.order_id, reason=str(args["reason"])
            )
        )

    all_tools = [
        get_order,
        get_shipment,
        check_inventory,
        cancel_order,
        issue_refund,
        create_replacement,
        update_shipping_address,
        escalate_to_human,
    ]
    return {t.name: t for t in all_tools}


def build_server(tools: dict[str, SdkMcpTool[Any]], allowed: tuple[str, ...]) -> McpSdkServerConfig:
    """Expose only the eligible tools. A tool that isn't registered can't be called."""
    return create_sdk_mcp_server(
        name=SERVER_NAME, version="1.0.0", tools=[tools[name] for name in allowed]
    )
