"""End-to-end controller tests on the offline runtime (real DB, hooks and tools)."""

from __future__ import annotations

import json

from order_agent.contracts import (
    CustomerReply,
    PendingCall,
    Phase,
    ResolutionSummary,
    StopReason,
    utcnow,
)
from order_agent.controller import TicketController
from order_agent.offline import OfflineRuntime
from order_agent.policy import qualified
from order_agent.runtime import StageOutcome


async def run(controller: TicketController, customer: str, message: str):
    state = controller.submit(customer, message)
    return await controller.advance(state.ticket_id)


def actions(store, ticket_id):
    return [r["action"] for r in store.ledger_for(ticket_id)]


async def test_status_question_takes_no_action(controller, store):
    s = await run(controller, "CUST-ALICE", "Where is ORD-1001? It hasn't arrived.")
    assert s.phase == Phase.COMPLETED and s.stop_reason == StopReason.RESOLVED
    assert s.triage.intent == "order_status"
    assert actions(store, s.ticket_id) == []
    assert s.reply is not None
    assert (controller.settings.artifacts_dir / s.ticket_id / "report.json").exists()


async def test_damaged_in_stock_gets_replacement(controller, store):
    s = await run(controller, "CUST-BOB", "ORD-1004 arrived and one MUG-01 is cracked")
    assert s.phase == Phase.COMPLETED
    assert actions(store, s.ticket_id) == ["replacement_created"]
    assert store.get_inventory("MUG-01")["reserved"] == 1


async def test_out_of_stock_large_refund_waits_for_human_then_completes(controller, store):
    s = await run(controller, "CUST-BOB", "My grinder from ORD-1003 arrived broken, GRINDER-03")
    assert s.phase == Phase.AWAITING_APPROVAL and s.approval_id
    assert store.get_order("ORD-1003").refunded_cents == 0
    assert controller.runtime.calls == ["triage", "resolve"]  # reply waits for the human

    s = await controller.decide_approval(s.approval_id, True, "ops-lead", "photo checks out")
    assert s.phase == Phase.COMPLETED and s.stop_reason == StopReason.RESOLVED
    assert store.get_order("ORD-1003").refunded_cents == 21900
    assert actions(store, s.ticket_id) == ["refund_pending_approval", "refund_issued"]
    assert "$219.00" in s.reply.body


async def test_rejected_approval_moves_no_money(controller, store):
    s = await run(controller, "CUST-BOB", "ORD-1003 grinder is damaged, GRINDER-03")
    s = await controller.decide_approval(s.approval_id, False, "ops-lead", "no evidence")
    assert s.phase == Phase.COMPLETED and s.stop_reason == StopReason.APPROVAL_REJECTED
    assert store.get_order("ORD-1003").refunded_cents == 0


async def test_cannot_touch_another_customers_order(controller, store):
    s = await run(controller, "CUST-ALICE", "Refund ORD-1003 in full please")
    assert s.stop_reason == StopReason.ORDER_NOT_FOUND and s.phase == Phase.COMPLETED
    assert actions(store, s.ticket_id) == []
    assert "resolve" not in controller.runtime.calls


async def test_cancel_unshipped_order(controller, store):
    s = await run(controller, "CUST-ALICE", "Please cancel ORD-1002, I ordered by mistake")
    assert s.phase == Phase.COMPLETED
    assert actions(store, s.ticket_id) == ["order_cancelled"]
    assert store.get_order("ORD-1002").status.value == "cancelled"


async def test_max_turns_after_side_effect_escalates(store, settings):
    """error_max_turns must gate the handoff even if the agent already acted."""

    async def resolve_then_run_out(spec):
        await spec.policy.pre_tool_use(
            {
                "tool_name": qualified("issue_refund"),
                "tool_input": {"order_id": "ORD-1001", "amount_cents": 1800, "reason": "x"},
            },
            "t",
            None,
        )
        await spec.tool_impls[qualified("issue_refund")](
            {"order_id": "ORD-1001", "amount_cents": 1800, "reason": "x"}
        )
        return StageOutcome(
            ok=False,
            subtype="error_max_turns",
            is_error=True,
            cost_usd=0.02,
            error="stage ended with error_max_turns",
        )

    c = TicketController(store, OfflineRuntime({"resolve": resolve_then_run_out}), settings)
    s = await run(c, "CUST-ALICE", "Refund $18 on ORD-1001, a mug was chipped")
    assert s.phase == Phase.ESCALATED and s.stop_reason == StopReason.STAGE_FAILED
    assert "already applied" in s.notes[-1]
    assert s.reply is None


async def test_summary_that_disagrees_with_ledger_is_rejected(store, settings):
    def liar(spec):
        return StageOutcome(
            ok=True,
            subtype="success",
            is_error=False,
            cost_usd=0.01,
            output=ResolutionSummary(
                outcome="resolved",
                actions_claimed=["refund_issued"],
                explanation="refunded everything",
            ),
        )

    c = TicketController(store, OfflineRuntime({"resolve": liar}), settings)
    s = await run(c, "CUST-ALICE", "Refund ORD-1001 please")
    assert s.stop_reason == StopReason.VERIFICATION_FAILED
    assert any("no such action" in n for n in s.notes)


async def test_reply_with_invented_amount_is_retried(store, settings):
    attempts = []

    def reply(spec):
        attempts.append(spec.context["feedback"])
        body = "We refunded you $999.00" if len(attempts) == 1 else "Your order is on its way."
        return StageOutcome(
            ok=True,
            subtype="success",
            is_error=False,
            cost_usd=0.001,
            output=CustomerReply(subject="Update", body=body),
        )

    c = TicketController(store, OfflineRuntime({"reply": reply}), settings)
    s = await run(c, "CUST-ALICE", "Where is ORD-1001?")
    assert s.phase == Phase.COMPLETED
    assert attempts[0] == [] and any("$999.00" in f for f in attempts[1])


async def test_crash_during_resolve_is_not_retried(controller, store):
    state = controller.submit("CUST-ALICE", "Refund ORD-1001")
    loaded, version = store.load_ticket(state.ticket_id)
    loaded.phase = Phase.RESOLVING
    loaded.pending_call = PendingCall(stage="resolve", attempt=1, written_at=utcnow())
    store.save_ticket(loaded, version)

    s = await controller.advance(state.ticket_id)
    assert s.phase == Phase.ESCALATED and s.stop_reason == StopReason.INTERRUPTED_CALL
    assert s.cost_accounting_complete is False
    assert s.pending_call is not None  # kept for inspection
    assert controller.runtime.calls == []


async def test_crash_during_triage_is_retried(controller, store):
    state = controller.submit("CUST-ALICE", "Where is ORD-1001?")
    loaded, version = store.load_ticket(state.ticket_id)
    loaded.phase = Phase.TRIAGING
    loaded.started_at = utcnow()
    loaded.pending_call = PendingCall(stage="triage", attempt=1, written_at=utcnow())
    store.save_ticket(loaded, version)

    s = await controller.advance(state.ticket_id)
    # Retried, but with unknown spend the controller refuses further paid calls.
    assert s.phase == Phase.ESCALATED and s.stop_reason == StopReason.EXECUTION_LIMIT
    assert "cost accounting" in s.notes[-1]


async def test_budget_exhaustion_stops_before_next_call(store, settings):
    c = TicketController(store, OfflineRuntime(cost_per_call=0.60), settings)
    s = await run(c, "CUST-ALICE", "Where is ORD-1001?")
    assert s.phase == Phase.ESCALATED and s.stop_reason == StopReason.EXECUTION_LIMIT
    assert c.runtime.calls == ["triage"]


async def test_submit_is_idempotent(controller):
    a = controller.submit("CUST-ALICE", "hi", idempotency_key="k-1")
    b = controller.submit("CUST-ALICE", "hi again", idempotency_key="k-1")
    assert a.ticket_id == b.ticket_id


async def test_lease_blocks_second_worker(controller, store):
    state = controller.submit("CUST-ALICE", "Where is ORD-1001?")
    assert store.acquire_lease(state.ticket_id, "someone-else", 60)
    s = await controller.advance(state.ticket_id)
    assert s.phase == Phase.RECEIVED and controller.runtime.calls == []


async def test_report_contains_audit_trail(controller):
    s = await run(controller, "CUST-BOB", "ORD-1004 arrived and one MUG-01 is cracked")
    report = json.loads(
        (controller.settings.artifacts_dir / s.ticket_id / "report.json").read_text()
    )
    events = [e["event"] for e in report["audit"]]
    assert "eligibility" in events and "tool_requested" in events and "tool_result" in events
