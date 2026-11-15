"""Sessions: continue a conversation in-process, then resume it later by session id.

    python examples/sessions_demo.py

Session history lives with the Claude Code runtime. Business state (orders, refunds,
the ledger) lives in your database. Resuming a session restores what was *said*, not
what was *done*; reload business state from the database whenever you resume.
"""

from __future__ import annotations

import asyncio

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, query


def base_options(**extra) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model="haiku",
        tools=[],
        setting_sources=[],
        permission_mode="dontAsk",
        max_turns=2,
        max_budget_usd=0.05,
        **extra,
    )


async def main() -> None:
    session_id = None
    async with ClaudeSDKClient(options=base_options()) as client:
        await client.query("Remember this order id: ORD-1001. Reply 'noted'.")
        async for msg in client.receive_response():
            if isinstance(msg, ResultMessage):
                session_id = msg.session_id
                print("turn 1:", msg.result)
        await client.query("Which order id did I give you?")
        async for msg in client.receive_response():
            if isinstance(msg, ResultMessage):
                print("turn 2 (same client):", msg.result)

    # Later: a new process, a new client, same conversation.
    async for msg in query(
        prompt="Repeat the order id once more.", options=base_options(resume=session_id)
    ):
        if isinstance(msg, ResultMessage):
            print("resumed:", msg.result, "| session", msg.session_id)


if __name__ == "__main__":
    asyncio.run(main())
