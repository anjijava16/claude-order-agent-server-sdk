"""Deterministic checks on model output.

The model's account of what happened is never trusted on its own. The action ledger is
written in the same transaction as each side effect, so it is the source of truth.
These checks are the order-system equivalent of the article's quote matching: they
confirm the model's claims against something the model can't edit.
"""

from __future__ import annotations

import re
from typing import Any

from .contracts import CustomerReply, ResolutionSummary

_MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d{2}))?")


def money_mentions(text: str) -> list[int]:
    out = []
    for whole, cents in _MONEY.findall(text):
        out.append(int(whole.replace(",", "")) * 100 + int(cents or 0))
    return out


def verify_resolution(summary: ResolutionSummary, ledger: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    done = {row["action"] for row in ledger}
    # An approved-later refund shows up as refund_issued after refund_pending_approval;
    # at resolve time only the pending row exists.
    for claim in summary.actions_claimed:
        if claim not in done:
            problems.append(f"claimed '{claim}' but the ledger has no such action")
    for action in done:
        if action not in summary.actions_claimed:
            problems.append(f"ledger has '{action}' but the summary doesn't mention it")
    if summary.outcome == "pending_approval" and "refund_pending_approval" not in done:
        problems.append("outcome is pending_approval but no refund is parked for approval")
    if "refund_pending_approval" in done and summary.outcome != "pending_approval":
        problems.append("a refund is parked for approval but outcome isn't pending_approval")
    if summary.outcome == "escalated" and "escalated" not in done:
        problems.append("outcome is escalated but escalate_to_human was never called")
    return problems


def verify_reply(
    reply: CustomerReply, ledger: list[dict[str, Any]], order_total_cents: int | None
) -> list[str]:
    problems: list[str] = []
    text = f"{reply.subject}\n{reply.body}"
    allowed = {row["amount_cents"] for row in ledger if row["amount_cents"] is not None}
    if order_total_cents is not None:
        allowed.add(order_total_cents)
    for cents in money_mentions(text):
        if cents not in allowed:
            problems.append(f"reply mentions ${cents / 100:.2f}, which isn't in the facts")
    actions = {row["action"] for row in ledger}
    issued = "refund_issued" in actions or "order_cancelled" in actions
    if re.search(r"\b(have|has been|was|were)\s+refunded\b", text, re.I) and not issued:
        problems.append("reply says a refund happened but none was issued")
    return problems
