"""M5 crew checkpointing: kill the ShortUpdateCrew after two tasks, resume, and prove they weren't re-run.
$0."""

import json
import sqlite3
from pathlib import Path

import pytest
from crewai.events.event_bus import crewai_event_bus

from compintel import hooks
from compintel.crews import short_update
from compintel.crews.deep_dive import DeepDiveCrew
from compintel.models import Brief
from tests.deep_dive_script import WEEK2_REPORTS, DeepDiveScript
from tests.fakes import Rule, ScriptedLLM
from tests.test_short_update_crew import REPORTS, Script, brief


class Killed(Exception):
    """Stands in for Ctrl+C / a crashed process mid-run."""


def killed_at_render(script):
    def step(messages, response_model):
        if "Call render_brief" in json.dumps(messages, default=str):
            raise Killed("killed before rendering")
        return script(messages, response_model)

    return step


@pytest.fixture
def hooks_on():
    hooks.install()


def test_killed_run_leaves_one_checkpoint_per_finished_task(hooks_on):
    llm = ScriptedLLM(Rule(killed_at_render(Script([brief()]))))
    with pytest.raises(Exception, match="killed before rendering"):
        short_update.run(3, REPORTS, fake=llm)
    crewai_event_bus.flush()
    with sqlite3.connect(short_update.checkpoint_db()) as db:
        rows = db.execute("SELECT id FROM checkpoints ORDER BY rowid").fetchall()
    assert len(rows) == 2  # classify_signals and write_short_brief finished; render_draft didn't
    assert short_update.latest_checkpoint() == f"{short_update.checkpoint_db()}#{rows[-1][0]}"


def test_resume_skips_finished_tasks(hooks_on, monkeypatch):
    monkeypatch.delenv("CREWAI_DESERIALIZE_CALLBACKS", raising=False)
    first = ScriptedLLM(Rule(killed_at_render(Script([brief()]))))
    with pytest.raises(Exception, match="killed before rendering"):
        short_update.run(3, REPORTS, fake=first)
    crewai_event_bus.flush()
    roles_before = [c["role"] for c in first.calls]
    assert roles_before[:3] == ["Market Analyst", "Brief Writer", "Guardrail Agent"]

    second = ScriptedLLM(Rule(Script([brief()])))
    result = short_update.resume(fake=second)

    # Only the unfinished task ran: the writer rendering. No analyst, no second brief, no guardrail calls.
    assert [c["role"] for c in second.calls] == ["Brief Writer"]
    assert result.brief == Brief.model_validate(brief())  # restored from the checkpoint, not regenerated
    assert Path("briefs/drafts/week-03.md").read_text() == result.markdown
    assert result.markdown.startswith("# Week 03:")


def test_nothing_to_resume_is_an_error(hooks_on):
    with pytest.raises(ValueError, match="no ShortUpdateCrew checkpoint"):
        short_update.resume(fake=ScriptedLLM())


def test_deep_dive_checkpoints_still_fail_upstream(world):
    """crewai 1.15.33 can't serialize a snapshot while an agent executor is mid-run, which this crew always
    has
    (parallel analyses here). If this starts failing, upstream fixed it: re-enable DeepDiveCrew
    checkpointing."""
    from crewai import CheckpointConfig
    from crewai.events.types.checkpoint_events import CheckpointCompletedEvent, CheckpointFailedEvent
    from crewai.state.provider.sqlite_provider import SqliteProvider

    from compintel.crews.short_update import signals_input

    hooks.install()
    outcomes: list[str] = []
    crewai_event_bus.on(CheckpointCompletedEvent)(lambda s, e: outcomes.append("ok"))
    crewai_event_bus.on(CheckpointFailedEvent)(lambda s, e: outcomes.append("failed"))
    crew = DeepDiveCrew(variant="sequential", fake=ScriptedLLM(Rule(DeepDiveScript()))).crew()
    crew.knowledge_sources, crew.embedder = (
        None,
        None,
    )  # not under test; keeps the fake embedder out of snapshots
    for agent in crew.agents:
        agent.knowledge_sources, agent.embedder = None, None
    crew.checkpoint = CheckpointConfig(
        location=str(Path("dd_checkpoints.db").resolve()),
        provider=SqliteProvider(),
        on_events=["task_completed"],
    )
    crew.kickoff(inputs={"week": 2, "signals": signals_input(WEEK2_REPORTS)})
    crewai_event_bus.flush()
    assert "failed" in outcomes


def test_replay_reruns_from_a_task_with_the_earlier_outputs(hooks_on):
    from crewai.memory.storage.kickoff_task_outputs_storage import KickoffTaskOutputsSQLiteStorage

    from compintel.replay import replay

    first = ScriptedLLM(Rule(Script([brief()])))
    short_update.run(3, REPORTS, fake=first)
    stored = KickoffTaskOutputsSQLiteStorage().load()
    assert [row["output"]["agent"] for row in stored] == ["Market Analyst", "Brief Writer", "Brief Writer"]
    write_task_id = stored[1]["task_id"]

    second = ScriptedLLM(Rule(Script([brief(headline="Replayed")])))
    result = replay(write_task_id, fake=second)
    # classify_signals came from storage: the analyst never ran; the writer wrote again, then rendered.
    assert "Market Analyst" not in [c["role"] for c in second.calls]
    assert result.raw.startswith("# Week 03: Replayed")
