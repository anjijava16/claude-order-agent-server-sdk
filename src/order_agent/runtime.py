"""Agent runtime boundary.

`AgentRuntime` is the only place the Claude Agent SDK is touched. The controller hands it
a `StageSpec` and gets back a `StageOutcome`. Tests and the offline demo swap in
`order_agent.offline.OfflineRuntime`, which drives the same hooks and tool handlers
without a model.

What one stage run guarantees
-----------------------------
* No built-in tools (`tools=[]`), no inherited settings (`setting_sources=[]`), only the
  MCP servers we pass (`strict_mcp_config=True`), and `permission_mode="dontAsk"` so any
  call that isn't pre-approved is denied instead of hanging on a prompt.
* Bounded by `max_turns`, `max_budget_usd` (client-side estimate, can overshoot) and a
  wall-clock timeout we enforce ourselves.
* Success means: ResultMessage.subtype == "success", not is_error, and a structured output
  that re-validates against the Pydantic contract. `error_max_turns`,
  `error_max_budget_usd`, timeouts and missing output are all failures, so partial work
  never flows into the next stage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from .hooks import STRUCTURED_OUTPUT_TOOL, ToolPolicy
from .tools import Handler

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


@dataclass
class StageSpec(Generic[T]):
    name: str
    prompt: str
    system_prompt: str
    output_type: type[T]
    policy: ToolPolicy
    workdir: Path
    max_turns: int = 3
    budget_usd: float = 0.10
    timeout_s: float = 60.0
    model: str = "sonnet"
    allowed_tools: list[str] = field(default_factory=list)  # qualified MCP names
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    tool_impls: dict[str, Handler] = field(default_factory=dict)  # for offline runtimes
    context: dict[str, Any] = field(default_factory=dict)  # structured input, for offline


@dataclass
class StageOutcome(Generic[T]):
    ok: bool
    subtype: str
    is_error: bool
    output: T | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    duration_ms: int | None = None
    session_id: str | None = None
    terminal_reason: str | None = None
    error: str | None = None
    model: str | None = None
    permission_denials: int = 0


class AgentRuntime(Protocol):
    async def run(self, spec: StageSpec[T]) -> StageOutcome[T]: ...


def validate_output(spec: StageSpec[T], raw: Any) -> tuple[T | None, str | None]:
    if raw is None:
        return None, "missing_structured_output"
    try:
        if isinstance(raw, str):
            return spec.output_type.model_validate_json(raw), None
        return spec.output_type.model_validate(raw), None
    except ValidationError as e:
        return None, f"invalid_structured_output: {e.errors()[:3]}"


class ClaudeRuntime:
    """Real runtime backed by the Claude Agent SDK (spawns the Claude Code CLI)."""

    def __init__(self, artifacts_dir: Path) -> None:
        self.artifacts_dir = artifacts_dir

    def _options(self, spec: StageSpec[Any]) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        spec.workdir.mkdir(parents=True, exist_ok=True)
        return ClaudeAgentOptions(
            model=spec.model,
            system_prompt=spec.system_prompt,
            cwd=str(spec.workdir),
            tools=[],
            allowed_tools=[*spec.allowed_tools, STRUCTURED_OUTPUT_TOOL],
            mcp_servers=spec.mcp_servers,
            strict_mcp_config=True,
            setting_sources=[],
            permission_mode="dontAsk",
            max_turns=spec.max_turns,
            max_budget_usd=spec.budget_usd,
            output_format={
                "type": "json_schema",
                "schema": spec.output_type.model_json_schema(),
            },
            hooks=spec.policy.as_sdk_hooks(),
        )

    async def run(self, spec: StageSpec[T]) -> StageOutcome[T]:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeSDKClient,
            ClaudeSDKError,
            ResultMessage,
            SystemMessage,
            TextBlock,
            ToolUseBlock,
        )

        transcript = spec.workdir / "transcript.jsonl"
        started = time.monotonic()
        result: ResultMessage | None = None
        model: str | None = None

        def emit(kind: str, **data: Any) -> None:
            with transcript.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {"t": round(time.monotonic() - started, 3), "kind": kind, **data},
                        default=str,
                    )
                    + "\n"
                )

        try:
            options = self._options(spec)
            async with asyncio.timeout(spec.timeout_s):
                async with ClaudeSDKClient(options=options) as client:
                    await client.query(spec.prompt)
                    async for msg in client.receive_response():
                        if isinstance(msg, SystemMessage) and msg.subtype == "init":
                            model = msg.data.get("model")
                            emit("init", model=model)
                        elif isinstance(msg, AssistantMessage):
                            for block in msg.content:
                                if isinstance(block, TextBlock):
                                    emit("text", text=block.text[:2000])
                                elif isinstance(block, ToolUseBlock):
                                    emit("tool_use", name=block.name, input=block.input)
                        elif isinstance(msg, ResultMessage):
                            result = msg
        except TimeoutError:
            emit("timeout", seconds=spec.timeout_s)
            return StageOutcome(
                ok=False,
                subtype="timeout",
                is_error=True,
                error="stage timed out",
                duration_ms=int((time.monotonic() - started) * 1000),
                model=model,
                permission_denials=len(spec.policy.denials),
            )
        except ClaudeSDKError as e:
            emit("sdk_error", error=repr(e))
            return StageOutcome(
                ok=False,
                subtype="sdk_error",
                is_error=True,
                error=repr(e),
                model=model,
                permission_denials=len(spec.policy.denials),
            )

        if result is None:
            return StageOutcome(
                ok=False,
                subtype="no_result",
                is_error=True,
                error="stream ended without result",
                model=model,
            )

        emit(
            "result",
            subtype=result.subtype,
            is_error=result.is_error,
            cost=result.total_cost_usd,
            turns=result.num_turns,
            terminal_reason=getattr(result, "terminal_reason", None),
        )

        outcome: StageOutcome[T] = StageOutcome(
            ok=False,
            subtype=result.subtype,
            is_error=result.is_error,
            cost_usd=result.total_cost_usd,
            num_turns=result.num_turns,
            duration_ms=result.duration_ms,
            session_id=result.session_id,
            terminal_reason=getattr(result, "terminal_reason", None),
            model=model,
            permission_denials=len(spec.policy.denials),
        )
        if result.subtype != "success" or result.is_error:
            outcome.error = f"stage ended with {result.subtype}"
            return outcome

        parsed, problem = validate_output(spec, result.structured_output)
        if problem:
            outcome.error = problem
            return outcome
        outcome.ok = True
        outcome.output = parsed
        return outcome
