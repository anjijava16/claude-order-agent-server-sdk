"""Runtime configuration.

Settings come from the environment. A local `.env` is loaded with override=False so
values set by the shell, Docker, or the deployment platform always win.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Model alias passed to the SDK ("sonnet", "haiku", "opus" or a full model id).
    model: str = "sonnet"

    # Where SQLite lives and where per-ticket run artifacts are written.
    db_path: Path = Path("var/orders.db")
    artifacts_dir: Path = Path("var/artifacts")

    # Business policy.
    auto_refund_limit_cents: int = Field(default=5_000, ge=0)  # $50.00
    max_mutations_per_ticket: int = Field(default=2, ge=1, le=10)

    # Per-ticket execution limits (the controller enforces these across stages).
    ticket_max_cost_usd: float = Field(default=0.50, gt=0)
    ticket_max_elapsed_seconds: int = Field(default=180, ge=10)

    # Worker.
    worker_concurrency: int = Field(default=4, ge=1, le=64)
    lease_seconds: int = Field(default=300, ge=30)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(override=False)
    raw: dict[str, object] = {}
    mapping = {
        "CLAUDE_MODEL": "model",
        "ORDER_AGENT_DB": "db_path",
        "ORDER_AGENT_ARTIFACTS": "artifacts_dir",
        "AUTO_REFUND_LIMIT_CENTS": "auto_refund_limit_cents",
        "MAX_MUTATIONS_PER_TICKET": "max_mutations_per_ticket",
        "TICKET_MAX_COST_USD": "ticket_max_cost_usd",
        "TICKET_MAX_ELAPSED_SECONDS": "ticket_max_elapsed_seconds",
        "WORKER_CONCURRENCY": "worker_concurrency",
        "LEASE_SECONDS": "lease_seconds",
    }
    for env_name, field in mapping.items():
        value = _env(env_name)
        if value is not None:
            raw[field] = value
    return Settings.model_validate(raw)
