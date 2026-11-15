"""Ticket controller: Python owns the state machine, the SDK owns each stage.

    RECEIVED → TRIAGING → RESOLVING ─┬─→ REPLYING → COMPLETED
                                     ├─→ AWAITING_APPROVAL ──(human)──→ REPLYING
                                     └─→ ESCALATED
    any stage failure / limit / interrupted mutating call → ESCALATED

Crash safety (from Part 3 of the series, adapted to a database):
* Before every model call the controller writes a `pending_call` marker. The marker is
  cleared in the same checkpoint that records the stage's output and cost.
* On restart, a ticket with a marker was interrupted mid-call. Side-effect-free stages
  (triage, reply) are retried. The resolve stage is not: it may have refunded money, so
  the ticket is escalated with stop_reason=interrupted_call and
  cost_accounting_complete=False for a human to reconcile against the ledger.
* Checkpoints use optimistic concurrency + a lease so two workers can't race.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta
from pathlib import Path

from .config import Settings
from .contracts import (
    TERMINAL_PHASES,
    PendingCall,
    Phase,
    StageRecord,
    StopReason,
    TicketLimits,
    TicketState,
    utcnow,
)
from .db import DomainError, Store
from .policy import check_ownership, eligible_tools
from .runtime import AgentRuntime, StageOutcome
from .stages import reply_facts, reply_spec, resolve_spec, triage_spec
from .verify import verify_reply, verify_resolution

log = logging.getLogger(__name__)

SIDE_EFFECT_FREE = {"triage", "reply"}
MAX_REPLY_ATTEMPTS = 2


class TicketController:
    def __init__(
        self, store: Store, runtime: AgentRuntime, settings: Settings, owner: str | None = None
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.settings = settings
        self.owner = owner or f"worker-{uuid.uuid4().hex[:8]}"

    # ------------------------------------------------------------------ public API

    def submit(
        self, customer_id: str, message: str, idempotency_key: str | None = None
    ) -> TicketState:
        state = TicketState(
            ticket_id=f"TCK-{uuid.uuid4().hex[:12]}",
            customer_id=customer_id,
            message=message,
            limits=TicketLimits(
                max_cost_usd=self.settings.ticket_max_cost_usd,
                max_elapsed_seconds=self.settings.ticket_max_elapsed_seconds,
            ),
            created_at=utcnow(),
        )
        return self.store.create_ticket(state, idempotency_key)

    async def advance(self, ticket_id: str) -> TicketState:
        """Run the ticket until it is terminal or waiting on a human."""
        if not self.store.acquire_lease(ticket_id, self.owner, self.settings.lease_seconds):
            loaded = self.store.load_ticket(ticket_id)
            if loaded is None:
                raise KeyError(ticket_id)
            log.info("ticket %s is leased by another worker", ticket_id)
            return loaded[0]
        try:
            loaded = self.store.load_ticket(ticket_id)
            if loaded is None:
                raise KeyError(ticket_id)
            state, version = loaded
            state, version = self._recover(state, version)
            while state.phase not in TERMINAL_PHASES and state.phase != Phase.AWAITING_APPROVAL:
                state, version = await self._step(state, version)
            if state.phase in TERMINAL_PHASES:
                self._write_report(state)
            return state
        finally:
            self.store.release_lease(ticket_id, self.owner)

    async def decide_approval(
        self, approval_id: str, approve: bool, reviewer: str, note: str | None
    ) -> TicketState:
        result = self.store.decide_refund_approval(
            approval_id=approval_id, approve=approve, reviewer=reviewer, note=note
        )
        ticket_id = result["ticket_id"]
        loaded = self.store.load_ticket(ticket_id)
        if loaded is None:
            raise KeyError(ticket_id)
        state, version = loaded
        if state.phase != Phase.AWAITING_APPROVAL:
            raise DomainError(f"ticket is {state.phase.value}, not awaiting approval")
        state = state.model_copy(deep=True)
        if approve:
            state.notes.append(f"refund approved by {reviewer}")
        else:
            state.notes.append(f"refund declined by {reviewer}: {note or 'no reason given'}")
            state.stop_reason = StopReason.APPROVAL_REJECTED
        state.phase = Phase.REPLYING
        self.store.save_ticket(state, version)
        self.store.audit(ticket_id, "approval_decided", result)
        return await self.advance(ticket_id)

    # ------------------------------------------------------------------ internals

    def _commit(self, state: TicketState, version: int) -> tuple[TicketState, int]:
        return state, self.store.save_ticket(state, version)

    def _recover(self, state: TicketState, version: int) -> tuple[TicketState, int]:
        pc = state.pending_call
        if pc is None:
            return state, version
        state = state.model_copy(deep=True)
        self.store.audit(state.ticket_id, "recovered_pending_call", pc.model_dump(mode="json"))
        state.cost_accounting_complete = False  # the interrupted call's spend is unknown
        if pc.stage in SIDE_EFFECT_FREE:
            state.pending_call = None
            state.notes.append(f"retrying interrupted {pc.stage} call")
            return self._commit(state, version)
        state.phase = Phase.ESCALATED
        state.stop_reason = StopReason.INTERRUPTED_CALL
        state.notes.append(
            f"{pc.stage} call was interrupted; reconcile the action ledger before retrying"
        )
        # Keep the marker for inspection, as in the article.
        return self._commit(state, version)

    def _remaining(self, state: TicketState) -> tuple[float, float]:
        budget = state.limits.max_cost_usd - state.estimated_cost_usd
        started = state.started_at or utcnow()
        deadline = started + timedelta(seconds=state.limits.max_elapsed_seconds)
        return budget, (deadline - utcnow()).total_seconds()

    def _limit_problem(self, state: TicketState) -> str | None:
        if not state.cost_accounting_complete:
            return "cost accounting is incomplete; refusing to start another paid call"
        budget, seconds = self._remaining(state)
        if budget <= 0.001:
            return "ticket cost budget exhausted"
        if seconds <= 5:
            return "ticket time limit reached"
        return None

    def _workdir(self, state: TicketState, stage: str, attempt: int) -> Path:
        return self.settings.artifacts_dir / state.ticket_id / f"{stage}-{attempt}"

    def _attempt(self, state: TicketState, stage: str) -> int:
        return 1 + sum(1 for s in state.stages if s.stage == stage)

    async def _call(
        self, state: TicketState, version: int, stage: str, spec_factory
    ) -> tuple[TicketState, int, StageOutcome]:
        """pending marker → model call → one commit with result + cost + cleared marker."""
        attempt = self._attempt(state, stage)
        state = state.model_copy(deep=True)
        state.pending_call = PendingCall(stage=stage, attempt=attempt, written_at=utcnow())
        state, version = self._commit(state, version)

        budget, seconds = self._remaining(state)
        spec = spec_factory(self._workdir(state, stage, attempt), budget, seconds, attempt)
        started = utcnow()
        outcome = await self.runtime.run(spec)

        state = state.model_copy(deep=True)
        state.pending_call = None
        if outcome.cost_usd is None:
            # Failed or timed-out calls can still cost money; we just don't know how much.
            if outcome.subtype not in ("sdk_error",):
                state.cost_accounting_complete = False
        else:
            state.estimated_cost_usd = round(state.estimated_cost_usd + outcome.cost_usd, 6)
        state.stages.append(
            StageRecord(
                stage=stage,
                attempt=attempt,
                subtype=outcome.subtype,
                is_error=outcome.is_error,
                num_turns=outcome.num_turns,
                cost_usd=outcome.cost_usd,
                duration_ms=outcome.duration_ms,
                session_id=outcome.session_id,
                permission_denials=outcome.permission_denials,
                started_at=started,
                finished_at=utcnow(),
            )
        )
        self.store.audit(
            state.ticket_id,
            "stage_finished",
            {
                "stage": stage,
                "attempt": attempt,
                "ok": outcome.ok,
                "subtype": outcome.subtype,
                "error": outcome.error,
                "cost_usd": outcome.cost_usd,
            },
        )
        return state, version, outcome

    def _stop(
        self, state: TicketState, version: int, phase: Phase, reason: StopReason, note: str
    ) -> tuple[TicketState, int]:
        state = state.model_copy(deep=True)
        state.phase = phase
        state.stop_reason = reason
        state.notes.append(note)
        return self._commit(state, version)

    async def _step(self, state: TicketState, version: int) -> tuple[TicketState, int]:
        if state.phase == Phase.RECEIVED:
            state = state.model_copy(deep=True)
            state.started_at = utcnow()  # set just before first dispatch
            state.phase = Phase.TRIAGING
            return self._commit(state, version)

        if (problem := self._limit_problem(state)) is not None:
            return self._stop(state, version, Phase.ESCALATED, StopReason.EXECUTION_LIMIT, problem)

        if state.phase == Phase.TRIAGING:
            return await self._triage(state, version)
        if state.phase == Phase.RESOLVING:
            return await self._resolve(state, version)
        if state.phase == Phase.REPLYING:
            return await self._reply(state, version)
        raise RuntimeError(f"no handler for phase {state.phase}")

    async def _triage(self, state: TicketState, version: int) -> tuple[TicketState, int]:
        state, version, out = await self._call(
            state,
            version,
            "triage",
            lambda wd, b, s, a: triage_spec(state, self.store, self.settings, wd, b, s, a),
        )
        if not out.ok:
            return self._stop(
                state,
                version,
                Phase.ESCALATED,
                StopReason.STAGE_FAILED,
                f"triage failed: {out.error}",
            )
        state.triage = out.output
        order = self.store.get_order(out.output.order_id) if out.output.order_id else None
        if check_ownership(order, state.customer_id):
            state.stop_reason = StopReason.ORDER_NOT_FOUND
            state.notes.append("no matching order for this customer; asking for the order id")
            state.phase = Phase.REPLYING
        else:
            state.phase = Phase.RESOLVING
        return self._commit(state, version)

    async def _resolve(self, state: TicketState, version: int) -> tuple[TicketState, int]:
        assert state.triage and state.triage.order_id
        order = self.store.get_order(state.triage.order_id)
        assert order is not None
        elig = eligible_tools(state.triage, order)
        self.store.audit(
            state.ticket_id, "eligibility", {"tools": list(elig.tools), "notes": list(elig.notes)}
        )
        state, version, out = await self._call(
            state,
            version,
            "resolve",
            lambda wd, b, s, a: resolve_spec(
                state, order, elig, self.store, self.settings, wd, b, s, a
            ),
        )
        ledger = self.store.ledger_for(state.ticket_id)
        if not out.ok:
            why = f"resolve failed ({out.subtype}): {out.error}"
            if ledger:
                why += f"; {len(ledger)} action(s) already applied, see ledger"
            return self._stop(state, version, Phase.ESCALATED, StopReason.STAGE_FAILED, why)

        state.resolution = out.output
        problems = verify_resolution(out.output, ledger)
        if problems:
            state.notes.extend(problems)
            return self._stop(
                state,
                version,
                Phase.ESCALATED,
                StopReason.VERIFICATION_FAILED,
                "resolution summary disagrees with the action ledger",
            )

        pending = [r for r in ledger if r["action"] == "refund_pending_approval"]
        if pending:
            state.approval_id = pending[-1]["detail"]["approval_id"]
            state.phase = Phase.AWAITING_APPROVAL
            state.notes.append(f"waiting on approval {state.approval_id}")
        else:
            state.phase = Phase.REPLYING
        return self._commit(state, version)

    async def _reply(self, state: TicketState, version: int) -> tuple[TicketState, int]:
        ledger = self.store.ledger_for(state.ticket_id)
        order = (
            self.store.get_order(state.triage.order_id)
            if state.triage
            and state.triage.order_id
            and state.stop_reason != StopReason.ORDER_NOT_FOUND
            else None
        )
        facts = reply_facts(state, ledger, order)
        feedback: list[str] = []
        for _ in range(MAX_REPLY_ATTEMPTS):
            if (problem := self._limit_problem(state)) is not None:
                return self._stop(
                    state, version, Phase.ESCALATED, StopReason.EXECUTION_LIMIT, problem
                )
            fb = list(feedback)
            state, version, out = await self._call(
                state,
                version,
                "reply",
                lambda wd, b, s, a, fb=fb, st=state: reply_spec(
                    st, facts, self.store, self.settings, wd, b, s, a, fb
                ),
            )
            if not out.ok:
                state, version = self._commit(state, version)
                feedback = [f"previous attempt failed: {out.error}"]
                continue
            feedback = verify_reply(out.output, ledger, order.total_cents if order else None)
            if not feedback:
                state.reply = out.output
                escalated = any(r["action"] == "escalated" for r in ledger)
                if escalated:
                    state.phase, state.stop_reason = Phase.ESCALATED, StopReason.NEEDS_HUMAN
                elif state.stop_reason in (
                    StopReason.APPROVAL_REJECTED,
                    StopReason.ORDER_NOT_FOUND,
                ):
                    state.phase = Phase.COMPLETED
                else:
                    state.phase, state.stop_reason = Phase.COMPLETED, StopReason.RESOLVED
                return self._commit(state, version)
            state.notes.extend(f"reply check: {p}" for p in feedback)
            state, version = self._commit(state, version)
        return self._stop(
            state,
            version,
            Phase.ESCALATED,
            StopReason.VERIFICATION_FAILED,
            "customer reply failed verification twice",
        )

    def _write_report(self, state: TicketState) -> None:
        out = self.settings.artifacts_dir / state.ticket_id
        out.mkdir(parents=True, exist_ok=True)
        report = {
            "ticket": json.loads(state.model_dump_json()),
            "ledger": self.store.ledger_for(state.ticket_id),
            "audit": self.store.audit_for(state.ticket_id),
        }
        tmp = out / "report.json.tmp"
        tmp.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        tmp.replace(out / "report.json")  # same directory → atomic rename
