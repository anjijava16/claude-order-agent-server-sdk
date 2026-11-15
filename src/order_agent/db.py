"""SQLite-backed store for orders, tickets, approvals and the action ledger.

Design notes
------------
* Every state-changing operation runs inside one `BEGIN IMMEDIATE` transaction and
  writes its ledger row in that same transaction. If the process dies mid-call, either
  both the side effect and its ledger row exist or neither does.
* Mutations carry an idempotency key (`<ticket>:<action>:<order>`). Retrying a tool call
  returns the original result instead of refunding twice.
* Ticket checkpoints use optimistic concurrency (`version`) plus a lease, so two workers
  can't advance the same ticket. This replaces the file lock used in the article series.
* SQLite keeps the example self-contained. The repository surface is small on purpose so
  it can be swapped for Postgres without touching the agent code.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .contracts import Order, OrderStatus, TicketState, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    status TEXT NOT NULL,
    lines_json TEXT NOT NULL,
    total_cents INTEGER NOT NULL,
    refunded_cents INTEGER NOT NULL DEFAULT 0,
    shipping_address TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inventory (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    on_hand INTEGER NOT NULL CHECK (on_hand >= 0),
    reserved INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0)
);
CREATE TABLE IF NOT EXISTS shipments (
    order_id TEXT PRIMARY KEY REFERENCES orders(order_id),
    carrier TEXT NOT NULL,
    tracking_number TEXT NOT NULL,
    status TEXT NOT NULL,
    eta TEXT
);
CREATE TABLE IF NOT EXISTS refunds (
    refund_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    ticket_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS replacements (
    replacement_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    ticket_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    reviewer TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    state_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until TEXT,
    idempotency_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    action TEXT NOT NULL,
    order_id TEXT NOT NULL,
    amount_cents INTEGER,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    event TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ledger_ticket ON action_ledger(ticket_id);
CREATE INDEX IF NOT EXISTS ix_audit_ticket ON audit_events(ticket_id);
CREATE INDEX IF NOT EXISTS ix_tickets_phase ON tickets(phase);
"""


class ConcurrencyError(RuntimeError):
    """Checkpoint version mismatch: someone else advanced this ticket."""


class DomainError(ValueError):
    """A business rule rejected the operation. Message is safe to show the model."""


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _one(self, sql: str, *args: Any) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def _all(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # ------------------------------------------------------------------ orders

    def get_order(self, order_id: str) -> Order | None:
        row = self._one("SELECT * FROM orders WHERE order_id = ?", order_id)
        return _row_to_order(row) if row else None

    def get_shipment(self, order_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM shipments WHERE order_id = ?", order_id)
        return dict(row) if row else None

    def get_inventory(self, sku: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM inventory WHERE sku = ?", sku)
        if not row:
            return None
        data = dict(row)
        data["available"] = data["on_hand"] - data["reserved"]
        return data

    def cancel_order(self, *, ticket_id: str, order_id: str, reason: str) -> dict[str, Any]:
        key = f"{ticket_id}:cancel:{order_id}"
        with self.tx() as c:
            prior = c.execute(
                "SELECT detail_json FROM action_ledger WHERE ticket_id=? "
                "AND action='order_cancelled' AND order_id=?",
                (ticket_id, order_id),
            ).fetchone()
            if prior:
                return {**json.loads(prior["detail_json"]), "idempotent_replay": True}
            row = c.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
            if not row:
                raise DomainError(f"order {order_id} not found")
            order = _row_to_order(row)
            if order.status not in (OrderStatus.PLACED, OrderStatus.PAID):
                raise DomainError(f"order {order_id} is {order.status.value}; cannot cancel")
            c.execute(
                "UPDATE orders SET status=?, refunded_cents=total_cents WHERE order_id=?",
                (OrderStatus.CANCELLED.value, order_id),
            )
            for line in order.lines:
                c.execute(
                    "UPDATE inventory SET reserved = MAX(reserved - ?, 0) WHERE sku=?",
                    (line.qty, line.sku),
                )
            detail = {
                "order_id": order_id,
                "status": "cancelled",
                "refunded_cents": order.refundable_cents,
                "reason": reason,
                "idempotency_key": key,
            }
            _ledger(c, ticket_id, "order_cancelled", order_id, order.refundable_cents, detail)
            return detail

    def create_refund(
        self,
        *,
        ticket_id: str,
        order_id: str,
        amount_cents: int,
        reason: str,
        auto_limit_cents: int,
    ) -> dict[str, Any]:
        """Issue a refund, or park it for approval when it exceeds the auto limit."""
        key = f"{ticket_id}:refund:{order_id}"
        with self.tx() as c:
            existing = c.execute("SELECT * FROM refunds WHERE idempotency_key=?", (key,)).fetchone()
            if existing:
                out = dict(existing)
                out["idempotent_replay"] = True
                return out
            row = c.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
            if not row:
                raise DomainError(f"order {order_id} not found")
            order = _row_to_order(row)
            if order.status == OrderStatus.CANCELLED:
                raise DomainError("order is cancelled; it was already refunded in full")
            if amount_cents <= 0:
                raise DomainError("amount_cents must be positive")
            if amount_cents > order.refundable_cents:
                raise DomainError(
                    f"amount {amount_cents} exceeds refundable balance {order.refundable_cents}"
                )
            refund_id = f"RF-{uuid.uuid4().hex[:10]}"
            now = utcnow().isoformat()
            if amount_cents <= auto_limit_cents:
                status = "issued"
                c.execute(
                    "UPDATE orders SET refunded_cents = refunded_cents + ? WHERE order_id=?",
                    (amount_cents, order_id),
                )
            else:
                status = "pending_approval"
            c.execute(
                "INSERT INTO refunds VALUES (?,?,?,?,?,?,?,?)",
                (refund_id, order_id, ticket_id, amount_cents, reason, status, key, now),
            )
            detail: dict[str, Any] = {
                "refund_id": refund_id,
                "order_id": order_id,
                "amount_cents": amount_cents,
                "status": status,
            }
            if status == "pending_approval":
                approval_id = f"AP-{uuid.uuid4().hex[:10]}"
                c.execute(
                    "INSERT INTO approvals (approval_id, ticket_id, kind, payload_json, status,"
                    " created_at) VALUES (?,?,?,?,?,?)",
                    (approval_id, ticket_id, "refund", json.dumps(detail), "pending", now),
                )
                detail["approval_id"] = approval_id
                _ledger(c, ticket_id, "refund_pending_approval", order_id, amount_cents, detail)
            else:
                _ledger(c, ticket_id, "refund_issued", order_id, amount_cents, detail)
            return detail

    def create_replacement(
        self, *, ticket_id: str, order_id: str, sku: str, qty: int
    ) -> dict[str, Any]:
        key = f"{ticket_id}:replace:{order_id}:{sku}"
        with self.tx() as c:
            existing = c.execute(
                "SELECT * FROM replacements WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing:
                return {**dict(existing), "idempotent_replay": True}
            row = c.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
            if not row:
                raise DomainError(f"order {order_id} not found")
            order = _row_to_order(row)
            ordered = {ln.sku: ln.qty for ln in order.lines}
            if sku not in ordered:
                raise DomainError(f"sku {sku} is not on order {order_id}")
            if qty > ordered[sku]:
                raise DomainError(f"qty {qty} exceeds ordered qty {ordered[sku]}")
            inv = c.execute("SELECT * FROM inventory WHERE sku=?", (sku,)).fetchone()
            if not inv or inv["on_hand"] - inv["reserved"] < qty:
                raise DomainError(f"sku {sku} is out of stock; offer a refund instead")
            c.execute("UPDATE inventory SET reserved = reserved + ? WHERE sku=?", (qty, sku))
            rid = f"RP-{uuid.uuid4().hex[:10]}"
            c.execute(
                "INSERT INTO replacements VALUES (?,?,?,?,?,?,?)",
                (rid, order_id, ticket_id, sku, qty, key, utcnow().isoformat()),
            )
            detail = {"replacement_id": rid, "order_id": order_id, "sku": sku, "qty": qty}
            _ledger(c, ticket_id, "replacement_created", order_id, None, detail)
            return detail

    def update_address(self, *, ticket_id: str, order_id: str, address: str) -> dict[str, Any]:
        with self.tx() as c:
            row = c.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
            if not row:
                raise DomainError(f"order {order_id} not found")
            order = _row_to_order(row)
            if order.status not in (OrderStatus.PLACED, OrderStatus.PAID, OrderStatus.PACKED):
                raise DomainError(f"order is {order.status.value}; address can no longer change")
            c.execute("UPDATE orders SET shipping_address=? WHERE order_id=?", (address, order_id))
            detail = {"order_id": order_id, "old": order.shipping_address, "new": address}
            _ledger(c, ticket_id, "address_updated", order_id, None, detail)
            return detail

    def record_escalation(self, *, ticket_id: str, order_id: str, reason: str) -> dict[str, Any]:
        with self.tx() as c:
            detail = {"reason": reason}
            _ledger(c, ticket_id, "escalated", order_id or "-", None, detail)
            return detail

    # ------------------------------------------------------------------ approvals

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM approvals WHERE approval_id=?", approval_id)
        return dict(row) if row else None

    def list_pending_approvals(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._all("SELECT * FROM approvals WHERE status='pending'")]

    def decide_refund_approval(
        self, *, approval_id: str, approve: bool, reviewer: str, note: str | None
    ) -> dict[str, Any]:
        """Deterministic: a human decision executes the parked refund. No model involved."""
        with self.tx() as c:
            ap = c.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if not ap:
                raise DomainError("approval not found")
            if ap["status"] != "pending":
                raise DomainError(f"approval already {ap['status']}")
            payload = json.loads(ap["payload_json"])
            now = utcnow().isoformat()
            new_status = "approved" if approve else "rejected"
            c.execute(
                "UPDATE approvals SET status=?, reviewer=?, note=?, decided_at=? "
                "WHERE approval_id=?",
                (new_status, reviewer, note, now, approval_id),
            )
            if approve:
                order = _row_to_order(
                    c.execute(
                        "SELECT * FROM orders WHERE order_id=?", (payload["order_id"],)
                    ).fetchone()
                )
                if payload["amount_cents"] > order.refundable_cents:
                    raise DomainError("refundable balance changed; re-run the ticket")
                c.execute(
                    "UPDATE orders SET refunded_cents = refunded_cents + ? WHERE order_id=?",
                    (payload["amount_cents"], payload["order_id"]),
                )
                c.execute(
                    "UPDATE refunds SET status='issued' WHERE refund_id=?", (payload["refund_id"],)
                )
                _ledger(
                    c,
                    ap["ticket_id"],
                    "refund_issued",
                    payload["order_id"],
                    payload["amount_cents"],
                    {**payload, "status": "issued", "approved_by": reviewer},
                )
            else:
                c.execute(
                    "UPDATE refunds SET status='rejected' WHERE refund_id=?",
                    (payload["refund_id"],),
                )
            return {"approval_id": approval_id, "status": new_status, "ticket_id": ap["ticket_id"]}

    # ------------------------------------------------------------------ ledger / audit

    def ledger_for(self, ticket_id: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT action, order_id, amount_cents, detail_json, created_at FROM action_ledger "
            "WHERE ticket_id=? ORDER BY id",
            ticket_id,
        )
        return [{**dict(r), "detail": json.loads(r["detail_json"])} for r in rows]

    def audit(self, ticket_id: str, event: str, detail: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO audit_events (ticket_id, event, detail_json, created_at) "
                "VALUES (?,?,?,?)",
                (ticket_id, event, json.dumps(detail, default=str), utcnow().isoformat()),
            )

    def audit_for(self, ticket_id: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT event, detail_json, created_at FROM audit_events WHERE ticket_id=? ORDER BY id",
            ticket_id,
        )
        return [
            {"event": r["event"], "detail": json.loads(r["detail_json"]), "at": r["created_at"]}
            for r in rows
        ]

    # ------------------------------------------------------------------ tickets

    def create_ticket(self, state: TicketState, idempotency_key: str | None) -> TicketState:
        with self.tx() as c:
            if idempotency_key:
                row = c.execute(
                    "SELECT state_json FROM tickets WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if row:
                    return TicketState.model_validate_json(row["state_json"])
            now = utcnow().isoformat()
            c.execute(
                "INSERT INTO tickets (ticket_id, customer_id, phase, state_json, version,"
                " idempotency_key, created_at, updated_at) VALUES (?,?,?,?,0,?,?,?)",
                (
                    state.ticket_id,
                    state.customer_id,
                    state.phase.value,
                    state.model_dump_json(),
                    idempotency_key,
                    now,
                    now,
                ),
            )
            return state

    def load_ticket(self, ticket_id: str) -> tuple[TicketState, int] | None:
        row = self._one("SELECT state_json, version FROM tickets WHERE ticket_id=?", ticket_id)
        if not row:
            return None
        return TicketState.model_validate_json(row["state_json"]), int(row["version"])

    def save_ticket(self, state: TicketState, expected_version: int) -> int:
        """Atomic checkpoint with optimistic concurrency. Returns the new version."""
        TicketState.model_validate(state.model_dump())  # never persist an invalid state
        with self.tx() as c:
            cur = c.execute(
                "UPDATE tickets SET state_json=?, phase=?, version=version+1, updated_at=? "
                "WHERE ticket_id=? AND version=?",
                (
                    state.model_dump_json(),
                    state.phase.value,
                    utcnow().isoformat(),
                    state.ticket_id,
                    expected_version,
                ),
            )
            if cur.rowcount != 1:
                raise ConcurrencyError(f"ticket {state.ticket_id} changed underneath us")
            return expected_version + 1

    def acquire_lease(self, ticket_id: str, owner: str, seconds: int) -> bool:
        now = utcnow()
        until = (now + timedelta(seconds=seconds)).isoformat()
        with self.tx() as c:
            cur = c.execute(
                "UPDATE tickets SET lease_owner=?, lease_until=? WHERE ticket_id=? AND "
                "(lease_owner IS NULL OR lease_owner=? OR lease_until < ?)",
                (owner, until, ticket_id, owner, now.isoformat()),
            )
            return cur.rowcount == 1

    def release_lease(self, ticket_id: str, owner: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE tickets SET lease_owner=NULL, lease_until=NULL "
                "WHERE ticket_id=? AND lease_owner=?",
                (ticket_id, owner),
            )

    def tickets_in_phases(self, phases: list[str]) -> list[str]:
        marks = ",".join("?" * len(phases))
        rows = self._all(f"SELECT ticket_id FROM tickets WHERE phase IN ({marks})", *phases)
        return [r["ticket_id"] for r in rows]

    # ------------------------------------------------------------------ seed

    def seed(self, data: dict[str, Any]) -> None:
        with self.tx() as c:
            for item in data["inventory"]:
                c.execute(
                    "INSERT OR REPLACE INTO inventory VALUES (?,?,?,?)",
                    (item["sku"], item["name"], item["on_hand"], item.get("reserved", 0)),
                )
            for o in data["orders"]:
                total = sum(ln["qty"] * ln["unit_price_cents"] for ln in o["lines"])
                c.execute(
                    "INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?)",
                    (
                        o["order_id"],
                        o["customer_id"],
                        o["status"],
                        json.dumps(o["lines"]),
                        total,
                        o.get("refunded_cents", 0),
                        o["shipping_address"],
                        o["created_at"],
                    ),
                )
            for s in data["shipments"]:
                c.execute(
                    "INSERT OR REPLACE INTO shipments VALUES (?,?,?,?,?)",
                    (s["order_id"], s["carrier"], s["tracking_number"], s["status"], s.get("eta")),
                )


def _row_to_order(row: sqlite3.Row) -> Order:
    return Order(
        order_id=row["order_id"],
        customer_id=row["customer_id"],
        status=OrderStatus(row["status"]),
        lines=json.loads(row["lines_json"]),
        total_cents=row["total_cents"],
        refunded_cents=row["refunded_cents"],
        shipping_address=row["shipping_address"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _ledger(
    c: sqlite3.Connection,
    ticket_id: str,
    action: str,
    order_id: str,
    amount_cents: int | None,
    detail: dict[str, Any],
) -> None:
    c.execute(
        "INSERT INTO action_ledger (ticket_id, action, order_id, amount_cents, detail_json,"
        " created_at) VALUES (?,?,?,?,?,?)",
        (ticket_id, action, order_id, amount_cents, json.dumps(detail), utcnow().isoformat()),
    )


SEED_DATA: dict[str, Any] = {
    "inventory": [
        {"sku": "MUG-01", "name": "Ceramic mug", "on_hand": 40},
        {"sku": "KETTLE-02", "name": "Gooseneck kettle", "on_hand": 6},
        {"sku": "GRINDER-03", "name": "Burr grinder", "on_hand": 0},
        {"sku": "BEANS-04", "name": "Single-origin beans 1kg", "on_hand": 120},
    ],
    "orders": [
        {
            "order_id": "ORD-1001",
            "customer_id": "CUST-ALICE",
            "status": "shipped",
            "lines": [
                {"sku": "MUG-01", "name": "Ceramic mug", "qty": 2, "unit_price_cents": 1800},
                {
                    "sku": "BEANS-04",
                    "name": "Single-origin beans 1kg",
                    "qty": 1,
                    "unit_price_cents": 3200,
                },
            ],
            "shipping_address": "12 Walnut St, Philadelphia, PA 19106",
            "created_at": "2026-11-09T14:02:00+00:00",
        },
        {
            "order_id": "ORD-1002",
            "customer_id": "CUST-ALICE",
            "status": "paid",
            "lines": [
                {
                    "sku": "KETTLE-02",
                    "name": "Gooseneck kettle",
                    "qty": 1,
                    "unit_price_cents": 8900,
                },
            ],
            "shipping_address": "12 Walnut St, Philadelphia, PA 19106",
            "created_at": "2026-11-13T09:30:00+00:00",
        },
        {
            "order_id": "ORD-1003",
            "customer_id": "CUST-BOB",
            "status": "delivered",
            "lines": [
                {"sku": "GRINDER-03", "name": "Burr grinder", "qty": 1, "unit_price_cents": 21900},
            ],
            "shipping_address": "400 Market St, Philadelphia, PA 19106",
            "created_at": "2026-11-02T18:45:00+00:00",
        },
        {
            "order_id": "ORD-1004",
            "customer_id": "CUST-BOB",
            "status": "delivered",
            "lines": [
                {"sku": "MUG-01", "name": "Ceramic mug", "qty": 4, "unit_price_cents": 1800},
            ],
            "shipping_address": "400 Market St, Philadelphia, PA 19106",
            "created_at": "2026-11-05T11:10:00+00:00",
        },
    ],
    "shipments": [
        {
            "order_id": "ORD-1001",
            "carrier": "UPS",
            "tracking_number": "1Z999AA10123456784",
            "status": "in_transit",
            "eta": "2026-11-17",
        },
        {
            "order_id": "ORD-1003",
            "carrier": "FedEx",
            "tracking_number": "794644790138",
            "status": "delivered",
            "eta": None,
        },
        {
            "order_id": "ORD-1004",
            "carrier": "USPS",
            "tracking_number": "9400111899223197428490",
            "status": "delivered",
            "eta": None,
        },
    ],
}
