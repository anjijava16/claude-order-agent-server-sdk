from order_agent.contracts import TriageResult
from order_agent.policy import check_ownership, eligible_tools, qualified


def triage(intent: str, **kw) -> TriageResult:
    return TriageResult(intent=intent, order_id="ORD-1001", summary="x", **kw)


def test_status_request_gets_read_only_tools(store):
    order = store.get_order("ORD-1001")
    elig = eligible_tools(triage("order_status"), order)
    assert set(elig.tools) == {"get_order", "get_shipment", "check_inventory", "escalate_to_human"}


def test_cancel_blocked_once_shipped(store):
    order = store.get_order("ORD-1001")  # shipped
    elig = eligible_tools(triage("cancel_order"), order)
    assert "cancel_order" not in elig.tools
    assert any("shipped" in n for n in elig.notes)


def test_refund_before_shipping_becomes_cancellation(store):
    order = store.get_order("ORD-1002")  # paid
    elig = eligible_tools(triage("refund_request"), order)
    assert "cancel_order" in elig.tools and "issue_refund" not in elig.tools


def test_damaged_item_allows_replacement_and_refund(store):
    order = store.get_order("ORD-1003")  # delivered
    elig = eligible_tools(triage("damaged_item"), order)
    assert {"create_replacement", "issue_refund"} <= set(elig.tools)
    assert qualified("issue_refund") == "mcp__orders__issue_refund"


def test_ownership_hides_other_customers_orders(store):
    order = store.get_order("ORD-1003")  # Bob's
    assert check_ownership(order, "CUST-ALICE") == "order_not_found"
    assert check_ownership(order, "CUST-BOB") is None
    assert check_ownership(None, "CUST-BOB") == "order_not_found"
