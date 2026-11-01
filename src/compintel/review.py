"""Who reviews the brief, and how (spec §10). Both are crewai `HumanFeedbackProvider`s; the Flow's
@human_feedback step calls whichever is installed as `flow_config.hitl_provider`.

- ConsoleReview (blocking): crewai's ConsoleProvider, shown the rendered brief instead of the Brief's repr.
- PendingReview (async): writes runs/pending_reviews/<flow_id>.json and raises HumanFeedbackPending.
  crewai then saves the state and the pending context (SQLite), and kickoff() *returns* the
  HumanFeedbackPending. Any later process continues it: `compintel review <flow_id> "feedback"`, which is
  Flow.from_pending(id).resume(feedback).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from crewai.flow.async_feedback import ConsoleProvider, HumanFeedbackPending, PendingFeedbackContext
from crewai.flow.flow_config import flow_config

from compintel.config import get_settings
from compintel.models import Brief
from compintel.tools.render_brief import render_markdown


def as_brief(output: Any) -> Brief:
    """The reviewed output as a Brief. After an async pause it comes back from JSON as a dict (crewai
    docs)."""
    return output if isinstance(output, Brief) else Brief.model_validate(output)


class ConsoleReview(ConsoleProvider):
    def request_feedback(self, context: PendingFeedbackContext, flow: Any) -> str:
        shown = dataclasses.replace(context, method_output=render_markdown(as_brief(context.method_output)))
        return super().request_feedback(shown, flow)


def pending_dir() -> Path:
    return get_settings().p3_runs_dir / "pending_reviews"


class PendingReview:
    """Non-blocking review: leave the brief where a reviewer can find it, then pause the flow."""

    def request_feedback(self, context: PendingFeedbackContext, flow: Any) -> str:
        brief = as_brief(context.method_output)
        path = pending_dir() / f"{context.flow_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "flow_id": context.flow_id,
                    "week": brief.week,
                    "message": context.message,
                    "outcomes": context.emit,
                    "requested_at": context.requested_at.isoformat(),
                    "markdown": render_markdown(brief),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        # crewai persists the flow state and this context itself; the file is only the reviewer's inbox.
        raise HumanFeedbackPending(context=context, callback_info={"file": str(path)})


def use(provider: Any) -> Any:
    """Install `provider` for every @human_feedback step without its own; returns the previous one."""
    previous = flow_config.hitl_provider
    flow_config.hitl_provider = provider
    return previous


def list_pending() -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(pending_dir().glob("*.json"))]


def resume(flow_id: str, feedback: str) -> tuple[Any, Any]:
    """Continue a paused week in this process. A further revise pauses again (a new pending file)."""
    from compintel.flow import CompetitiveIntelFlow
    from compintel.runtime import bootstrap

    bootstrap()
    path = pending_dir() / f"{flow_id}.json"
    if not path.exists():
        raise ValueError(f"no pending review {flow_id!r}; see `compintel pending`")
    request = path.read_text()
    flow = CompetitiveIntelFlow.from_pending(flow_id)  # type: ignore[attr-defined]  # @persist hides it
    previous = use(PendingReview())
    try:
        path.unlink()  # answered; PendingReview writes a new one if the flow pauses again
        result = flow.resume(feedback)
    except Exception:
        # Failed before crewai recorded the answer: still pending there, so put the inbox file back too.
        if not path.exists() and flow.persistence.load_pending_feedback(flow_id) is not None:
            path.write_text(request)
        raise
    finally:
        use(previous)
    return flow, result
