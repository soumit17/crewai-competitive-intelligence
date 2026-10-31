"""ShortUpdateCrew glue: load the JSON-first crew (crew.jsonc + agents/*.jsonc) and run it on scout reports.

Everything about the crew's shape lives in the JSON files; this module only supplies inputs and the callables
the JSON points at (`save_draft`; guardrails live in compintel.guardrails).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from crewai import Crew, CrewOutput, TaskOutput
from crewai.project.crew_loader import load_crew
from crewai.utilities.pydantic_schema_utils import generate_model_description

from compintel import hooks, llms
from compintel.guardrails import brief_from
from compintel.models import Brief, SignalReport, ThreatAssessments
from compintel.tools.render_brief import draft_path

CREW_FILE = Path(__file__).parent / "crew.jsonc"
# The writer has a tool, so crewai's loop sends it no response schema; the task prompt carries it instead
# (same fix as the M2 scouts, built by the crewai helper its Converter uses).
BRIEF_SCHEMA = json.dumps(generate_model_description(Brief)["json_schema"]["schema"])


def save_draft(output: TaskOutput) -> None:
    """write_short_brief's task callback: persist the approved Brief for RenderBriefTool (and for review)."""
    brief = brief_from(output)
    path = draft_path(brief.week)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(brief.model_dump_json(indent=2))


def signals_input(reports: dict[str, SignalReport]) -> str:
    """Compact signals for the prompt: the fields the analyst and writer need, nothing else (tokens)."""
    rows = [
        {
            "id": s.id,
            "company": s.company,
            "kind": s.kind,
            "detail": s.detail,
            "credibility": s.source_credibility,
            "novelty": s.novelty,
            "impact": s.impact_on_us,
        }
        for r in reports.values()
        for s in r.signals
    ]
    return json.dumps(rows, ensure_ascii=False)


def load(final: bool = False, fake=None) -> Crew:
    # Fail closed: the writer's FileReadTool has crewai-tools' path containment switched off (see config).
    if not hooks.is_installed(hooks.file_read_allowlist):
        raise RuntimeError("file_read_allowlist hook not installed; call compintel.hooks.install() first")
    # The scope wraps the load: agents and string guardrails take their LLM when constructed.
    with llms.scope(final=final, fake=fake):
        crew, _defaults = load_crew(CREW_FILE)
    return crew


@dataclass
class ShortUpdateResult:
    brief: Brief
    threats: ThreatAssessments
    markdown: str
    output: CrewOutput


def run(week: int, reports: dict[str, SignalReport], final: bool = False, fake=None) -> ShortUpdateResult:
    previous = Path("briefs") / f"week-{week - 1:02d}.md"
    crew = load(final=final, fake=fake)
    output = crew.kickoff(
        inputs={
            "week": week,
            "week_nn": f"{week:02d}",
            "signals": signals_input(reports),
            "previous_brief": str(previous) if previous.exists() else "none",
            "brief_schema": BRIEF_SCHEMA,
        }
    )
    classify, write, _render = crew.tasks
    assert classify.output is not None and write.output is not None
    threats = classify.output.pydantic
    if not isinstance(threats, ThreatAssessments):
        threats = ThreatAssessments.model_validate_json(classify.output.raw)
    # CrewStreamingOutput only comes back with stream=True, which this crew doesn't set.
    assert isinstance(output, CrewOutput)
    return ShortUpdateResult(
        brief=brief_from(write.output), threats=threats, markdown=output.raw, output=output
    )
