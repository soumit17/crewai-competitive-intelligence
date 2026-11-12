"""ShortUpdateCrew glue: load the JSON-first crew (crew.jsonc + agents/*.jsonc) and run it on scout reports.

Everything about the crew's shape lives in the JSON files; this module only supplies inputs and the callables
the JSON points at (`save_draft`; guardrails live in compintel.guardrails).
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from crewai import Crew, CrewOutput, TaskOutput
from crewai.project.crew_loader import load_crew
from crewai.state.checkpoint_config import CheckpointConfig
from crewai.state.provider.sqlite_provider import SqliteProvider
from crewai.utilities.pydantic_schema_utils import generate_model_description

from compintel import hooks, llms
from compintel.config import get_settings
from compintel.guardrails import brief_from, register_signals
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


def checkpoint_db() -> Path:
    path = get_settings().p3_runs_dir / "checkpoints" / "short_update.db"
    path.parent.mkdir(parents=True, exist_ok=True)  # SqliteProvider doesn't create the folder
    return path


def checkpoint_config() -> CheckpointConfig:
    """crew.jsonc's `checkpoint` (via crew_refs): one snapshot per finished task, the last 20 kept."""
    return CheckpointConfig(
        location=str(checkpoint_db()),
        provider=SqliteProvider(),
        on_events=["task_completed"],
        max_checkpoints=20,
    )


def latest_checkpoint() -> str | None:
    """`db_path#checkpoint_id` of the newest snapshot, the form SqliteProvider restores from."""
    db = checkpoint_db()
    if not db.exists() or db.stat().st_size == 0:
        return None
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT id FROM checkpoints ORDER BY rowid DESC LIMIT 1").fetchone()
    return f"{db}#{row[0]}" if row else None


def _restore(location: str, fork: bool, final: bool = False, fake=None) -> Crew:
    """A crew rebuilt from a snapshot (resumed, or forked onto a new branch), with what a restore drops put
    back.

    A restore rebuilds the crew from the snapshot, which does not bring back (crewai docs): callable
    guardrails, and our LLM objects and memory views as configured here; callbacks only with
    CREWAI_DESERIALIZE_CALLBACKS=1. They are re-attached from a freshly loaded crew. The signal registry is
    rebuilt from the saved inputs.
    """
    # Our own snapshot file, so its callback references (save_draft, register_signals) are trusted.
    os.environ["CREWAI_DESERIALIZE_CALLBACKS"] = "1"
    config = CheckpointConfig(restore_from=location)
    crew = Crew.fork(config) if fork else Crew.from_checkpoint(config)
    fresh = load(final=final, fake=fake)
    fresh_agents = {a.role: a for a in fresh.agents}
    for agent in crew.agents:
        agent.llm = fresh_agents[agent.role].llm
        # A restored MemorySlice comes back unbound (a live Memory isn't serializable): use a fresh one.
        agent.memory = fresh_agents[agent.role].memory
    fresh_tasks = {t.name: t for t in fresh.tasks}
    for task in crew.tasks:
        task.guardrails = fresh_tasks[task.name].guardrails  # type: ignore[assignment]
        # Rebuild crewai's guardrail list (and the string guardrail's LLM binding); a model validator method.
        task.ensure_guardrails_is_list_of_callables()  # type: ignore[operator]
    register_signals(crew.checkpoint_inputs or {})
    return crew


def resume(location: str | None = None, final: bool = False, fake=None) -> ShortUpdateResult:
    """Continue a killed run from its last checkpoint: finished tasks are skipped, not re-run."""
    location = location or latest_checkpoint()
    if location is None:
        raise ValueError("no ShortUpdateCrew checkpoint to resume from")
    crew = _restore(location, fork=False, final=final, fake=fake)
    output = crew.kickoff()
    assert isinstance(output, CrewOutput)
    return _result(crew, output)


def fork(location: str, overrides: dict[str, str], final: bool = False, fake=None) -> ShortUpdateResult:
    """What-if (spec §13): fork a snapshot, replace finished tasks' outputs, re-run what comes after them.

    `overrides`: task name -> new raw output. The same edit as the `crewai checkpoint` TUI's Fork: crewai's
    own `_apply_task_overrides` (sets the output, rewrites that agent's answer in its message history, clears
    every later task's output so it re-runs). The TUI sets only `.raw`; a typed output's `.pydantic` is
    rebuilt here from the new raw, so downstream readers of either see the same edit.
    """
    from crewai_cli.checkpoint_tui import _apply_task_overrides

    crew = _restore(location, fork=True, final=final, fake=fake)
    index = {t.name or f"task_{i + 1}": i for i, t in enumerate(crew.tasks)}
    unknown = sorted(set(overrides) - set(index))
    if unknown:
        raise ValueError(f"no task named {unknown} in this crew ({', '.join(index)})")
    unfinished = [name for name in overrides if crew.tasks[index[name]].output is None]
    if unfinished:
        raise ValueError(f"{unfinished} hadn't finished in this snapshot: nothing to override")
    _apply_task_overrides(crew, {index[name]: raw for name, raw in overrides.items()})
    for name, raw in overrides.items():
        task = crew.tasks[index[name]]
        if task.output is not None and task.output_pydantic is not None:
            task.output.pydantic = task.output_pydantic.model_validate_json(raw)
    output = crew.kickoff()
    assert isinstance(output, CrewOutput)
    return _result(crew, output)


def snapshots_since(rowid: int) -> list[tuple[int, str]]:
    """(rowid, `db#id`) of the snapshots written after `rowid`, oldest first: one per finished task."""
    db = checkpoint_db()
    if not db.exists() or db.stat().st_size == 0:
        return []
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT rowid, id FROM checkpoints WHERE rowid > ? ORDER BY rowid", (rowid,)
        ).fetchall()
    return [(r, f"{db}#{i}") for r, i in rows]


def last_rowid() -> int:
    snaps = snapshots_since(-1)
    return snaps[-1][0] if snaps else 0


def _result(crew: Crew, output: CrewOutput) -> ShortUpdateResult:
    classify, write, _render = crew.tasks
    assert classify.output is not None and write.output is not None
    threats = classify.output.pydantic
    if not isinstance(threats, ThreatAssessments):
        threats = ThreatAssessments.model_validate_json(classify.output.raw)
    return ShortUpdateResult(
        brief=brief_from(write.output), threats=threats, markdown=output.raw, output=output
    )


def inputs_for(week: int, reports: dict[str, SignalReport]) -> dict:
    previous = Path("briefs") / f"week-{week - 1:02d}.md"
    return {
        "week": week,
        "week_nn": f"{week:02d}",
        "signals": signals_input(reports),
        "previous_brief": str(previous) if previous.exists() else "none",
        "brief_schema": BRIEF_SCHEMA,
    }


def run(
    week: int,
    reports: dict[str, SignalReport],
    final: bool = False,
    fake=None,
    trained_agents_file: str | None = None,
) -> ShortUpdateResult:
    """`trained_agents_file`: a `crewai train` result (M8d) whose suggestions the agents follow."""
    crew = load(final=final, fake=fake)
    if trained_agents_file is not None:
        crew.trained_agents_file = trained_agents_file
    output = crew.kickoff(inputs=inputs_for(week, reports))
    # CrewStreamingOutput only comes back with stream=True, which this crew doesn't set.
    assert isinstance(output, CrewOutput)
    return _result(crew, output)
