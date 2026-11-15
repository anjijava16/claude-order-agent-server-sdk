"""Model-driven delegation with AgentDefinition (contrast with the controller's code-driven stages).

A lead agent answers an ops question about an order by delegating to two read-only
specialists through the built-in `Agent` tool. Nothing here can mutate state: the
specialists only see read-only MCP tools.

    python examples/subagents_demo.py ORD-1001

Use this shape for open-ended *investigation*. Keep money-moving workflows in the
Python controller, where stage order and limits are enforced by code.
"""

from __future__ import annotations

import asyncio
import sys

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from order_agent.config import get_settings
from order_agent.db import Store
from order_agent.policy import qualified
from order_agent.tools import ToolContext, build_server, build_tools

READ_ONLY = ("get_order", "get_shipment", "check_inventory")


async def main(order_id: str) -> None:
    settings = get_settings()
    store = Store(settings.db_path)
    order = store.get_order(order_id)
    if order is None:
        sys.exit(f"{order_id} not found; run `order-agent seed` first")

    ctx = ToolContext(store, settings, "ops-investigation", order.customer_id, order_id)
    server = build_server(build_tools(ctx), READ_ONLY)
    read_tools = [qualified(t) for t in READ_ONLY]

    agents = {
        "shipping-analyst": AgentDefinition(
            description="Investigates carrier status, ETA and delivery risk for one order.",
            prompt="You analyse shipments. Use get_shipment and get_order. Be factual.",
            tools=[qualified("get_shipment"), qualified("get_order")],
            model="haiku",
        ),
        "inventory-analyst": AgentDefinition(
            description="Checks whether each line on the order could be replaced from stock.",
            prompt="You check stock levels for every SKU on the order. Report available units.",
            tools=[qualified("get_order"), qualified("check_inventory")],
            model="haiku",
        ),
    }

    options = ClaudeAgentOptions(
        model=settings.model,
        system_prompt=(
            "You are an ops lead. Delegate to your specialists, then write a 5-line brief: "
            "shipment status, delivery risk, replacement feasibility per SKU, recommendation."
        ),
        tools=["Agent"],  # only the delegation tool from the built-in set
        allowed_tools=["Agent", *read_tools],
        agents=agents,
        mcp_servers={"orders": server},
        strict_mcp_config=True,
        setting_sources=[],
        permission_mode="dontAsk",
        max_turns=10,
        max_budget_usd=0.25,
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(f"Give me an ops brief on order {order_id}.")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(block.text)
            elif isinstance(msg, ResultMessage):
                print(f"\n[{msg.subtype}] turns={msg.num_turns} cost≈${msg.total_cost_usd}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "ORD-1001"))
