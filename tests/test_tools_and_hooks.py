import asyncio
import json

import pytest

from order_agent.db import DomainError
from order_agent.hooks import ToolPolicy
from order_agent.policy import qualified
from order_agent.tools import ToolContext, build_tools


def pre(policy: ToolPolicy, tool: str, args: dict):
    return policy.pre_tool_use({"tool_name": tool, "tool_input": args}, "tu-1", None)


def is_denied(decision: dict) -> bool:
    return decision.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


@pytest.fixture
def policy(store):
    return ToolPolicy(
        store=store,
        ticket_id="T1",
        stage="resolve",
        allowed={qualified("get_order"), qualified("issue_refund")},
        order_id="ORD-1001",
        max_mutations=1,
        refundable_cents=6800,
    )


async def test_unlisted_tool_is_denied_and_audited(policy, store):
    assert is_denied(await pre(policy, qualified("cancel_order"), {"order_id": "ORD-1001"}))
    assert is_denied(await pre(policy, "Bash", {"command": "ls"}))
    events = [e["event"] for e in store.audit_for("T1")]
    assert events.count("tool_denied") == 2


async def test_structured_output_always_allowed(policy):
    assert await pre(policy, "StructuredOutput", {"anything": 1}) == {}


async def test_order_pinning(policy):
    d = await pre(policy, qualified("get_order"), {"order_id": "ORD-1003"})
    assert is_denied(d)
    assert "pinned" in d["hookSpecificOutput"]["permissionDecisionReason"]


async def test_mutation_budget_and_refund_ceiling(policy):
    too_much = {"order_id": "ORD-1001", "amount_cents": 99_999, "reason": "x"}
    assert is_denied(await pre(policy, qualified("issue_refund"), too_much))
    ok = {"order_id": "ORD-1001", "amount_cents": 1800, "reason": "x"}
    assert await pre(policy, qualified("issue_refund"), ok) == {}
    assert is_denied(await pre(policy, qualified("issue_refund"), ok))  # budget of 1 used


async def test_parallel_mutations_cannot_both_pass(store):
    policy = ToolPolicy(
        store=store,
        ticket_id="T2",
        stage="resolve",
        allowed={qualified("issue_refund")},
        order_id="ORD-1001",
        max_mutations=1,
    )
    args = {"order_id": "ORD-1001", "amount_cents": 100, "reason": "x"}
    results = await asyncio.gather(
        *[pre(policy, qualified("issue_refund"), args) for _ in range(5)]
    )
    assert sum(not is_denied(r) for r in results) == 1


async def test_tool_errors_are_readable_for_self_correction(store, settings):
    tools = build_tools(ToolContext(store, settings, "T3", "CUST-ALICE", "ORD-1001"))
    res = await tools["create_replacement"].handler(
        {"order_id": "ORD-1001", "sku": "KETTLE-02", "qty": 1}
    )
    assert res["is_error"] is True and "not on order" in res["content"][0]["text"]
    res = await tools["get_order"].handler({"order_id": "ORD-1003"})
    assert res["is_error"] is True


async def test_refund_is_idempotent_per_ticket(store, settings):
    tools = build_tools(ToolContext(store, settings, "T4", "CUST-ALICE", "ORD-1001"))
    args = {"order_id": "ORD-1001", "amount_cents": 1800, "reason": "chipped mug"}
    first = json.loads((await tools["issue_refund"].handler(args))["content"][0]["text"])
    second = json.loads((await tools["issue_refund"].handler(args))["content"][0]["text"])
    assert first["status"] == "issued" and second["idempotent_replay"] is True
    assert store.get_order("ORD-1001").refunded_cents == 1800
    assert [r["action"] for r in store.ledger_for("T4")] == ["refund_issued"]


async def test_large_refund_is_parked_not_paid(store, settings):
    tools = build_tools(ToolContext(store, settings, "T5", "CUST-BOB", "ORD-1003"))
    out = json.loads(
        (
            await tools["issue_refund"].handler(
                {"order_id": "ORD-1003", "amount_cents": 21900, "reason": "broken"}
            )
        )["content"][0]["text"]
    )
    assert out["status"] == "pending_approval" and out["approval_id"].startswith("AP-")
    assert store.get_order("ORD-1003").refunded_cents == 0


def test_approval_rechecks_balance(store):
    store.create_refund(
        ticket_id="T6", order_id="ORD-1003", amount_cents=21900, reason="x", auto_limit_cents=0
    )
    ap = store.list_pending_approvals()[0]
    # Someone refunds manually in the meantime.
    store.create_refund(
        ticket_id="T7",
        order_id="ORD-1003",
        amount_cents=100,
        reason="goodwill",
        auto_limit_cents=10_000,
    )
    with pytest.raises(DomainError):
        store.decide_refund_approval(
            approval_id=ap["approval_id"], approve=True, reviewer="ops", note=None
        )
    assert store.get_approval(ap["approval_id"])["status"] == "pending"  # rolled back
