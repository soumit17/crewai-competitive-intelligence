"""DeepDiveCrew glue: build the crew for a variant and run it on scout reports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from crewai import CrewOutput
from pydantic import ValidationError

from compintel import hooks
from compintel.crews.deep_dive.deep_dive_crew import VARIANTS, DeepDiveCrew
from compintel.crews.short_update import signals_input
from compintel.guardrails import brief_from
from compintel.models import Brief, SignalReport, Synthesis

__all__ = ["VARIANTS", "DeepDiveCrew", "DeepDiveResult", "run"]


@dataclass
class DeepDiveResult:
    brief: Brief
    synthesis: Synthesis | None
    escalation_memo: str | None  # None when the ConditionalTask was skipped
    output: CrewOutput
    crew: Any  # the DeepDiveCrew instance (tasks, intended agents)


def run(
    week: int,
    reports: dict[str, SignalReport],
    variant: str = "manager_agent",
    final: bool = False,
    fake: Any = None,
    verbose: bool = False,
) -> DeepDiveResult:
    # Fail closed: the writer's FileReadTool has crewai-tools' path containment switched off (see config).
    if not hooks.is_installed(hooks.file_read_allowlist):
        raise RuntimeError("file_read_allowlist hook not installed; call compintel.hooks.install() first")
    # type: ignore: crewai's @CrewBase type stub hides the decorated class's own __init__ parameters.
    base: Any = DeepDiveCrew(variant=variant, final=final, fake=fake, verbose=verbose)  # type: ignore[call-arg]
    output = base.crew().kickoff(inputs={"week": week, "signals": signals_input(reports)})
    # CrewStreamingOutput only comes back with stream=True, which this crew doesn't set.
    assert isinstance(output, CrewOutput)
    by_name = {t.name: t for t in base.tasks}
    synth_out = by_name["synthesis"].output
    synthesis = None
    if synth_out is not None:
        try:
            synthesis = (
                synth_out.pydantic
                if isinstance(synth_out.pydantic, Synthesis)
                else Synthesis.model_validate_json(synth_out.raw)
            )
        except ValidationError:
            synthesis = None
    memo_out = by_name["escalation_memo"].output
    memo = memo_out.raw if memo_out is not None and memo_out.raw.strip() else None
    write_out = by_name["write_brief"].output
    assert write_out is not None
    return DeepDiveResult(
        brief=brief_from(write_out), synthesis=synthesis, escalation_memo=memo, output=output, crew=base
    )
