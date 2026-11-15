"""HTTP API.

POST /tickets                       → 202, ticket queued (Idempotency-Key header supported)
GET  /tickets/{id}                  → state, ledger, audit trail
GET  /approvals                     → pending human approvals
POST /approvals/{id}/approve|reject → human decision; resumes the ticket
GET  /orders/{id}                   → order snapshot (ops view)
GET  /healthz                       → liveness
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import Settings, get_settings
from .contracts import ApprovalDecision, CreateTicket
from .controller import TicketController
from .db import SEED_DATA, DomainError, Store
from .logging_setup import configure_logging
from .runtime import AgentRuntime, ClaudeRuntime
from .worker import WorkerPool


def build_runtime(settings: Settings) -> AgentRuntime:
    if os.environ.get("ORDER_AGENT_OFFLINE") == "1":
        from .offline import OfflineRuntime

        return OfflineRuntime()
    return ClaudeRuntime(settings.artifacts_dir)


def create_app(
    settings: Settings | None = None,
    runtime: AgentRuntime | None = None,
    store: Store | None = None,
    start_workers: bool = True,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        st = store or Store(settings.db_path)
        if os.environ.get("ORDER_AGENT_SEED") == "1":
            st.seed(SEED_DATA)
        controller = TicketController(st, runtime or build_runtime(settings), settings)
        pool = WorkerPool(controller, settings.worker_concurrency)
        app.state.store, app.state.controller, app.state.pool = st, controller, pool
        if start_workers:
            pool.start()
        try:
            yield
        finally:
            await pool.stop()

    app = FastAPI(title="Order Agent", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(DomainError)
    async def _domain(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/tickets", status_code=202)
    async def create_ticket(
        body: CreateTicket,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        controller: TicketController = request.app.state.controller
        state = controller.submit(body.customer_id, body.message, idempotency_key)
        request.app.state.pool.enqueue(state.ticket_id)
        return {"ticket_id": state.ticket_id, "phase": state.phase.value}

    @app.get("/tickets/{ticket_id}")
    async def get_ticket(ticket_id: str, request: Request) -> dict[str, Any]:
        st: Store = request.app.state.store
        loaded = st.load_ticket(ticket_id)
        if loaded is None:
            raise HTTPException(404, "ticket not found")
        state, version = loaded
        return {
            "ticket": state.model_dump(mode="json"),
            "version": version,
            "ledger": st.ledger_for(ticket_id),
            "audit": st.audit_for(ticket_id),
        }

    @app.get("/approvals")
    async def approvals(request: Request) -> list[dict[str, Any]]:
        return request.app.state.store.list_pending_approvals()

    async def _decide(approval_id: str, approve: bool, body: ApprovalDecision, request: Request):
        controller: TicketController = request.app.state.controller
        state = await controller.decide_approval(approval_id, approve, body.reviewer, body.note)
        return {
            "ticket_id": state.ticket_id,
            "phase": state.phase.value,
            "stop_reason": state.stop_reason.value if state.stop_reason else None,
        }

    @app.post("/approvals/{approval_id}/approve")
    async def approve(approval_id: str, body: ApprovalDecision, request: Request):
        return await _decide(approval_id, True, body, request)

    @app.post("/approvals/{approval_id}/reject")
    async def reject(approval_id: str, body: ApprovalDecision, request: Request):
        return await _decide(approval_id, False, body, request)

    @app.get("/orders/{order_id}")
    async def get_order(order_id: str, request: Request) -> dict[str, Any]:
        order = request.app.state.store.get_order(order_id)
        if order is None:
            raise HTTPException(404, "order not found")
        return {**order.model_dump(mode="json"), "refundable_cents": order.refundable_cents}

    return app


def get_app() -> FastAPI:
    """Uvicorn factory: `uvicorn order_agent.api:get_app --factory`."""
    return create_app()
