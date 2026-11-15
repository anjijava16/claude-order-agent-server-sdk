"""Typed boundaries between stages.

Every model forbids extra keys. Stage outputs that come from the model are generated
from these classes as JSON Schema (`output_format`) and then re-validated in Python,
so a malformed or partial answer never reaches the next stage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- domain


class OrderStatus(StrEnum):
    PLACED = "placed"
    PAID = "paid"
    PACKED = "packed"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class OrderLine(Contract):
    sku: str
    name: str
    qty: int = Field(ge=1)
    unit_price_cents: int = Field(ge=0)


class Order(Contract):
    order_id: str
    customer_id: str
    status: OrderStatus
    lines: list[OrderLine]
    total_cents: int = Field(ge=0)
    refunded_cents: int = Field(default=0, ge=0)
    shipping_address: str
    created_at: AwareDatetime

    @property
    def refundable_cents(self) -> int:
        return max(self.total_cents - self.refunded_cents, 0)


# --------------------------------------------------------------------------- stage I/O

Intent = Literal[
    "order_status",
    "cancel_order",
    "refund_request",
    "damaged_item",
    "change_address",
    "other",
]


class TriageResult(Contract):
    """Output of the triage stage (structured output, no tools)."""

    intent: Intent
    order_id: str | None = Field(
        default=None, description="Order id exactly as written by the customer, e.g. ORD-1001"
    )
    summary: str = Field(max_length=400, description="One-sentence neutral summary")
    urgency: Literal["low", "normal", "high"] = "normal"
    requested_refund_cents: int | None = Field(default=None, ge=0)
    damaged_skus: list[str] = Field(default_factory=list)
    new_address: str | None = None


class ResolutionSummary(Contract):
    """What the resolution agent says it did. Checked against the action ledger."""

    outcome: Literal["resolved", "pending_approval", "escalated", "no_action_needed"]
    actions_claimed: list[
        Literal[
            "refund_issued",
            "refund_pending_approval",
            "order_cancelled",
            "replacement_created",
            "address_updated",
            "escalated",
        ]
    ] = Field(default_factory=list)
    explanation: str = Field(max_length=800)


class CustomerReply(Contract):
    subject: str = Field(max_length=120)
    body: str = Field(max_length=2000)


# --------------------------------------------------------------------------- ticket state


class Phase(StrEnum):
    RECEIVED = "received"
    TRIAGING = "triaging"
    RESOLVING = "resolving"
    AWAITING_APPROVAL = "awaiting_approval"
    REPLYING = "replying"
    COMPLETED = "completed"
    ESCALATED = "escalated"
    FAILED = "failed"


TERMINAL_PHASES = {Phase.COMPLETED, Phase.ESCALATED, Phase.FAILED}


class StopReason(StrEnum):
    RESOLVED = "resolved"
    NEEDS_HUMAN = "needs_human"
    ORDER_NOT_FOUND = "order_not_found"
    POLICY_BLOCKED = "policy_blocked"
    VERIFICATION_FAILED = "verification_failed"
    EXECUTION_LIMIT = "execution_limit"
    STAGE_FAILED = "stage_failed"
    INTERRUPTED_CALL = "interrupted_call"
    APPROVAL_REJECTED = "approval_rejected"


class StageRecord(Contract):
    stage: str
    attempt: int
    subtype: str
    is_error: bool
    num_turns: int | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    session_id: str | None = None
    permission_denials: int = 0
    started_at: AwareDatetime
    finished_at: AwareDatetime


class PendingCall(Contract):
    """Written before a model call, cleared in the same commit as its result."""

    stage: str
    attempt: int
    written_at: AwareDatetime


class TicketLimits(Contract):
    max_cost_usd: float = Field(gt=0, allow_inf_nan=False)
    max_elapsed_seconds: int = Field(ge=10)


class TicketState(Contract):
    schema_version: Literal["1"] = "1"
    ticket_id: str
    customer_id: str
    message: str = Field(max_length=4000)
    phase: Phase = Phase.RECEIVED
    limits: TicketLimits
    created_at: AwareDatetime
    started_at: AwareDatetime | None = None
    estimated_cost_usd: float = 0.0
    cost_accounting_complete: bool = True
    pending_call: PendingCall | None = None
    triage: TriageResult | None = None
    resolution: ResolutionSummary | None = None
    reply: CustomerReply | None = None
    approval_id: str | None = None
    stages: list[StageRecord] = Field(default_factory=list)
    stop_reason: StopReason | None = None
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- API


class CreateTicket(Contract):
    customer_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=4000)


class ApprovalDecision(Contract):
    reviewer: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=500)
