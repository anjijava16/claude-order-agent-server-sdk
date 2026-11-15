"""Command line entry point: `order-agent <command>`.

  seed                         create the database and load demo orders
  ticket  --customer --message run one ticket to completion and print the result
  approve / reject <approval>  act on a parked refund
  resume                       finish any tickets left unfinished by a crash
  serve                        run the HTTP API + worker pool
  check-setup                  one cheap live call to prove the SDK, CLI and key work

Add --offline to ticket/approve/reject/resume to use the deterministic offline runtime.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from .config import get_settings
from .controller import TicketController
from .db import SEED_DATA, Store
from .logging_setup import configure_logging
from .runtime import AgentRuntime, ClaudeRuntime
from .worker import WorkerPool


def _runtime(offline: bool) -> AgentRuntime:
    if offline:
        from .offline import OfflineRuntime

        return OfflineRuntime()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set (or pass --offline)")
    return ClaudeRuntime(get_settings().artifacts_dir)


def _controller(offline: bool) -> TicketController:
    s = get_settings()
    return TicketController(Store(s.db_path), _runtime(offline), s)


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _summary(c: TicketController, ticket_id: str) -> dict[str, Any]:
    loaded = c.store.load_ticket(ticket_id)
    assert loaded
    state, _ = loaded
    return {
        "ticket_id": state.ticket_id,
        "phase": state.phase.value,
        "stop_reason": state.stop_reason.value if state.stop_reason else None,
        "intent": state.triage.intent if state.triage else None,
        "approval_id": state.approval_id,
        "estimated_cost_usd": state.estimated_cost_usd,
        "cost_accounting_complete": state.cost_accounting_complete,
        "actions": [(r["action"], r["amount_cents"]) for r in c.store.ledger_for(ticket_id)],
        "reply": state.reply.model_dump() if state.reply else None,
        "notes": state.notes,
    }


async def _ticket(args: argparse.Namespace) -> None:
    c = _controller(args.offline)
    state = c.submit(args.customer, args.message, args.idempotency_key)
    await c.advance(state.ticket_id)
    _print(_summary(c, state.ticket_id))


async def _decide(args: argparse.Namespace, approve: bool) -> None:
    c = _controller(args.offline)
    state = await c.decide_approval(args.approval_id, approve, args.reviewer, args.note)
    _print(_summary(c, state.ticket_id))


async def _resume(args: argparse.Namespace) -> None:
    c = _controller(args.offline)
    pool = WorkerPool(c, concurrency=1)
    n = pool.recover()
    while not pool.queue.empty():
        tid = pool.queue.get_nowait()
        await c.advance(tid)
        _print(_summary(c, tid))
    print(f"resumed {n} ticket(s)")


async def _check_setup() -> None:
    """Article-style probe: no tools, 1 turn, tiny budget, exact marker, hard timeout."""
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, SystemMessage, query

    s = get_settings()
    marker = "ORDER_AGENT_READY"
    opts = ClaudeAgentOptions(
        model=s.model,
        tools=[],
        setting_sources=[],
        max_turns=1,
        max_budget_usd=0.10,
        permission_mode="dontAsk",
        system_prompt=f"Reply with exactly: {marker}",
    )
    report: dict[str, Any] = {"model_requested": s.model}

    async def go() -> None:
        async for msg in query(prompt="ping", options=opts):
            if isinstance(msg, SystemMessage) and msg.subtype == "init":
                report["model_resolved"] = msg.data.get("model")
            elif isinstance(msg, ResultMessage):
                report.update(
                    subtype=msg.subtype,
                    result=msg.result,
                    cost_usd=msg.total_cost_usd,
                    ok=(msg.result or "").strip() == marker,
                )

    await asyncio.wait_for(go(), timeout=90)
    s.artifacts_dir.mkdir(parents=True, exist_ok=True)
    (s.artifacts_dir / "setup_check.json").write_text(json.dumps(report, indent=2))
    _print(report)
    if not report.get("ok"):
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    p = argparse.ArgumentParser(prog="order-agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("seed")

    t = sub.add_parser("ticket")
    t.add_argument("--customer", required=True)
    t.add_argument("--message", required=True)
    t.add_argument("--idempotency-key")
    t.add_argument("--offline", action="store_true")

    for name in ("approve", "reject"):
        a = sub.add_parser(name)
        a.add_argument("approval_id")
        a.add_argument("--reviewer", default="ops-oncall")
        a.add_argument("--note")
        a.add_argument("--offline", action="store_true")

    r = sub.add_parser("resume")
    r.add_argument("--offline", action="store_true")

    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8000)

    sub.add_parser("check-setup")

    args = p.parse_args(argv)
    match args.cmd:
        case "seed":
            s = get_settings()
            Store(s.db_path).seed(SEED_DATA)
            print(f"seeded {s.db_path}")
        case "ticket":
            asyncio.run(_ticket(args))
        case "approve":
            asyncio.run(_decide(args, True))
        case "reject":
            asyncio.run(_decide(args, False))
        case "resume":
            asyncio.run(_resume(args))
        case "serve":
            import uvicorn

            uvicorn.run("order_agent.api:get_app", factory=True, host=args.host, port=args.port)
        case "check-setup":
            asyncio.run(_check_setup())


if __name__ == "__main__":
    main()
