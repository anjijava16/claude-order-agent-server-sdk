from __future__ import annotations

from pathlib import Path

import pytest

from order_agent.config import Settings
from order_agent.controller import TicketController
from order_agent.db import SEED_DATA, Store
from order_agent.offline import OfflineRuntime


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        model="haiku",
        db_path=tmp_path / "orders.db",
        artifacts_dir=tmp_path / "artifacts",
        auto_refund_limit_cents=5_000,
        max_mutations_per_ticket=2,
        ticket_max_cost_usd=0.50,
        ticket_max_elapsed_seconds=120,
        worker_concurrency=2,
    )


@pytest.fixture
def store(settings: Settings) -> Store:
    s = Store(settings.db_path)
    s.seed(SEED_DATA)
    yield s
    s.close()


@pytest.fixture
def runtime() -> OfflineRuntime:
    return OfflineRuntime()


@pytest.fixture
def controller(store: Store, runtime: OfflineRuntime, settings: Settings) -> TicketController:
    return TicketController(store, runtime, settings, owner="test-worker")
