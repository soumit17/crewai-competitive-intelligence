"""Scripted model decisions for a whole DeepDiveCrew run ($0), shared by the deep-dive tests.

Answers are keyed by what a real model would see: the response_model crewai asks for (crew planner, agent
planning, LLM guardrail) or a marker from the task description. In the hierarchical variants the manager
delegates each task to its intended specialist through crewai's real "Delegate work to coworker" tool, then
returns that specialist's answer, which is what a well-behaved manager does.
"""

from __future__ import annotations

import json
import types
import typing
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel

from compintel.models import Signal, SignalReport
from tests.fakes import ToolCall, tool_results

MANAGER_ROLES = ("Head of Strategy", "Crew Manager")


def signal(
    sid: str, kind: str, detail: str, cred: float = 0.9, impact: str = "high", novelty: str = "new"
) -> Signal:
    company = sid.split("-")[0]
    return Signal(id=sid, company=company, kind=kind, detail=detail, source_ref="x",  # type: ignore[arg-type]
                  source_credibility=cred, novelty=novelty, impact_on_us=impact)  # type: ignore[arg-type]  # fmt: skip


WEEK2_REPORTS = {
    "StockSathi": SignalReport(company="StockSathi", week=2, summary="", signals=[
        signal("StockSathi-2-1", "pricing_change", "Pro cut 30% from ₹999 to ₹699/month"),
        signal("StockSathi-2-2", "feature_launch", "Launched UPI Udhaar store credit"),
    ]),
    "ShelfMitra": SignalReport(company="ShelfMitra", week=2, summary="", signals=[
        signal("ShelfMitra-2-1", "funding", "Raised a ₹200 crore Series B"),
        signal("ShelfMitra-2-2", "hiring_expansion", "15 sales roles in Kolkata"),
    ]),
    "BahiBook": SignalReport(company="BahiBook", week=2, summary="", signals=[
        signal("BahiBook-2-1", "other", "Fixed A5 invoice layout", impact="none"),
    ]),
}  # fmt: skip


def threats(high: bool = True) -> list[dict]:
    level = "high" if high else "medium"
    return [
        {"company": "StockSathi", "threat": level, "rationale": "30% price cut [StockSathi-2-1]", "signal_ids": ["StockSathi-2-1"]},
        {"company": "ShelfMitra", "threat": level, "rationale": "Kolkata sales hiring [ShelfMitra-2-2]", "signal_ids": ["ShelfMitra-2-2", "ShelfMitra-2-1"]},
        {"company": "BahiBook", "threat": "low", "rationale": "Bug fix only [BahiBook-2-1]", "signal_ids": ["BahiBook-2-1"]},
    ]  # fmt: skip


def synthesis(high: bool = True) -> dict:
    return {
        "threats": threats(high),
        "key_findings": ["ShelfMitra is hiring 15 sales staff in Kolkata [ShelfMitra-2-2]"],
        "recommendations": [{"action": "Defend top Kolkata accounts", "owner": "Sales", "urgency": "this_week", "signal_ids": ["ShelfMitra-2-2"]}],
    }  # fmt: skip


def brief(high: bool = True, **changes: Any) -> dict:
    return {
        "week": 2,
        "headline": "ShelfMitra moves on Kolkata; StockSathi undercuts Dukaan",
        "tl_dr": ["ShelfMitra is hiring 15 sales staff in Kolkata [ShelfMitra-2-2]", "StockSathi Pro now ₹699, under our ₹799 [StockSathi-2-1]"],
        "since_last_week": ["First brief: no earlier week to compare."],
        "threats": threats(high),
        "recommendations": synthesis()["recommendations"],
        "watchlist": ["UPI Udhaar adoption [StockSathi-2-2]"],
        "discounted_claims": [],
        "route": "material",
        **changes,
    }  # fmt: skip


# Task description marker -> (intended specialist, answer)
def answers(high: bool) -> dict[str, tuple[str, Any]]:
    return {
        "Analyse this week's competitor pricing": ("Pricing Analyst", "- StockSathi Pro ₹699, ₹100 (13%) under Dukaan [StockSathi-2-1]"),
        "Find where this week's competitor product": ("Product Analyst", "- UPI Udhaar overlaps Khata on UPI (Q2 2027) [StockSathi-2-2]"),
        "Assess how much each competitor": ("Market Analyst", {"threats": threats(high)}),
        "Combine the pricing, product and threat": ("Strategist", synthesis(high)),
        "Write a short escalation memo": ("Strategist", "# Escalation: Kolkata under attack\n\nShelfMitra is hiring [ShelfMitra-2-2]."),
        "Write the weekly brief": ("Brief Writer", None),  # filled from DeepDiveScript.briefs
    }  # fmt: skip


def fill(annotation: Any) -> Any:
    """A minimal valid value for a type: for crewai's own planning models, whose content the tests don't use."""
    origin, args = get_origin(annotation), get_args(annotation)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return {name: fill(f.annotation) for name, f in annotation.model_fields.items() if f.is_required()}
    if origin is Literal:
        return args[0]
    if origin in (list, typing.List):  # noqa: UP006
        return [fill(args[0])] if args else []
    if origin in (dict, typing.Dict):  # noqa: UP006
        return {}
    if origin in (typing.Union, types.UnionType):
        return fill(next(a for a in args if a is not type(None)))
    return {str: "step", int: 1, float: 0.5, bool: True}.get(annotation, "step")


class DeepDiveScript:
    def __init__(self, briefs: list[dict] | None = None, high: bool = True):
        self.answers = answers(high)
        self.briefs = list(briefs or [brief(high)])
        self.writer_prompts: list[str] = []
        self.delegations: list[str] = []
        self.query_rewrites = 0

    def _task(self, prompt: str) -> str | None:
        return next((m for m in self.answers if m in prompt), None)

    def __call__(self, messages: list[Any], response_model: Any) -> Any:
        prompt = json.dumps(messages, default=str)
        system = str(messages[0].get("content", "")) if messages and isinstance(messages[0], dict) else ""
        if response_model is not None:
            name = response_model.__name__
            if name == "LLMGuardrailResult":
                return {"valid": True, "feedback": None}
            if name == "PlannerTaskPydanticOutput":
                return {
                    "list_of_plans_per_task": [
                        {"task_number": i, "task": "t", "plan": " Plan: do it."} for i in range(1, 7)
                    ]
                }
            marker = self._task(prompt)
            if marker and isinstance(self.answers[marker][1], dict):
                return self.answers[marker][1]
            return fill(response_model)  # agent-planning internals
        if system.startswith("Your goal is to rewrite the user query"):
            # crewai's knowledge retrieval: one LLM call per task to turn the prompt into a search query.
            self.query_rewrites += 1
            return "KiranaCloud pricing roadmap Kolkata"
        marker = self._task(prompt)
        if any(system.startswith(f"You are {r}") for r in MANAGER_ROLES) and marker:
            done = tool_results(messages)
            if '"role": "tool"' not in done:
                coworker = self.answers[marker][0]
                self.delegations.append(coworker)
                return [
                    ToolCall(
                        "delegate_work_to_coworker",
                        {"task": marker, "context": "See task.", "coworker": coworker},
                    )
                ]
            return json.loads(done)[-1]["content"]  # the specialist's answer, as the final answer
        if marker == "Write the weekly brief":
            self.writer_prompts.append(prompt)
            return json.dumps(self.briefs.pop(0) if len(self.briefs) > 1 else self.briefs[0])
        if marker:
            answer = self.answers[marker][1]
            return answer if isinstance(answer, str) else json.dumps(answer)
        return "Done."  # agent-planning step execution and anything else unkeyed
