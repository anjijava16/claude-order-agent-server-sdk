from __future__ import annotations

import time

from fastapi.testclient import TestClient

from order_agent.api import create_app
from order_agent.contracts import TriageResult
from order_agent.hooks import ToolPolicy
from order_agent.offline import OfflineRuntime
from order_agent.runtime import ClaudeRuntime, StageSpec, validate_output


def test_sdk_options_are_least_privilege(store, settings, tmp_path):
    spec = StageSpec(
        name="triage",
        prompt="p",
        system_prompt="s",
        output_type=TriageResult,
        policy=ToolPolicy(store=store, ticket_id="T", stage="triage", allowed=set()),
        workdir=tmp_path / "w",
        max_turns=3,
        budget_usd=0.05,
        model="haiku",
    )
    opts = ClaudeRuntime(tmp_path)._options(spec)
    assert opts.tools == []
    assert opts.allowed_tools == ["StructuredOutput"]
    assert opts.permission_mode == "dontAsk"
    assert opts.setting_sources == []
    assert opts.strict_mcp_config is True
    assert opts.max_turns == 3 and opts.max_budget_usd == 0.05
    assert opts.output_format["type"] == "json_schema"
    assert "intent" in opts.output_format["schema"]["properties"]
    assert set(opts.hooks) == {"PreToolUse", "PostToolUse", "PostToolUseFailure"}


def test_structured_output_is_revalidated(store, tmp_path):
    spec = StageSpec(
        name="triage",
        prompt="",
        system_prompt="",
        output_type=TriageResult,
        policy=None,
        workdir=tmp_path,
    )  # type: ignore[arg-type]
    assert validate_output(spec, None)[1] == "missing_structured_output"
    assert validate_output(spec, {"intent": "bogus", "summary": "x"})[1].startswith("invalid")
    assert validate_output(spec, {"intent": "other", "summary": "x", "extra": 1})[0] is None
    ok, problem = validate_output(spec, {"intent": "other", "summary": "x"})
    assert problem is None and ok.intent == "other"


def test_http_flow_with_approval(store, settings):
    app = create_app(settings=settings, runtime=OfflineRuntime(), store=store)
    with TestClient(app) as client:
        r = client.post(
            "/tickets",
            json={"customer_id": "CUST-BOB", "message": "ORD-1003 grinder arrived broken"},
            headers={"Idempotency-Key": "abc"},
        )
        assert r.status_code == 202
        tid = r.json()["ticket_id"]
        again = client.post(
            "/tickets",
            json={"customer_id": "CUST-BOB", "message": "dup"},
            headers={"Idempotency-Key": "abc"},
        )
        assert again.json()["ticket_id"] == tid

        for _ in range(100):
            body = client.get(f"/tickets/{tid}").json()
            if body["ticket"]["phase"] == "awaiting_approval":
                break
            time.sleep(0.02)
        assert body["ticket"]["phase"] == "awaiting_approval"

        pending = client.get("/approvals").json()
        assert len(pending) == 1
        r = client.post(
            f"/approvals/{pending[0]['approval_id']}/approve", json={"reviewer": "ops-lead"}
        )
        assert r.json()["phase"] == "completed"
        again = client.post(
            f"/approvals/{pending[0]['approval_id']}/approve", json={"reviewer": "ops-lead"}
        )
        assert again.status_code == 409
        assert client.get("/orders/ORD-1003").json()["refunded_cents"] == 21900
