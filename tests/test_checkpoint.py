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


def edited_classification() -> str:
    """The what-if: StockSathi's analysis says the cut was 50% and the threat is high."""
    from compintel.models import ThreatAssessment, ThreatAssessments

    return ThreatAssessments(
        threats=[
            ThreatAssessment(company="StockSathi", threat="high", signal_ids=["StockSathi-3-1"],
                             rationale="WHAT-IF: Pro cut 50% to ₹499, below our ₹649 floor [StockSathi-3-1]"),
            ThreatAssessment(company="ShelfMitra", threat="medium", signal_ids=[], rationale="x"),
            ThreatAssessment(company="BahiBook", threat="low", signal_ids=[], rationale="x"),
        ]
    ).model_dump_json()  # fmt: skip


def test_fork_with_an_edited_task_output_reruns_only_what_follows(hooks_on):
    """Spec §13's what-if, as the `crewai checkpoint` TUI's Fork does it: edit a finished task's output on a
    fork, and only the tasks after it run again, reading the edit."""
    short_update.run(3, REPORTS, fake=ScriptedLLM(Rule(Script([brief()]))))
    crewai_event_bus.flush()
    snaps = short_update.snapshots_since(0)
    assert len(snaps) == 3  # one per task
    after_classify = snaps[0][1]

    script = Script([brief(headline="StockSathi's 50% cut")])
    second = ScriptedLLM(Rule(script))
    result = short_update.fork(after_classify, {"classify_signals": edited_classification()}, fake=second)

    roles = [c["role"] for c in second.calls]
    assert "Market Analyst" not in roles and roles.count("Brief Writer") == 2  # write + render, not classify
    assert "WHAT-IF: Pro cut 50%" in script.writer_prompts[0]  # the writer read the edited analysis
    assert result.threats.threats[0].threat == "high"  # .pydantic carries the edit, not only .raw
    assert result.brief.headline == "StockSathi's 50% cut"
    assert len(short_update.snapshots_since(snaps[-1][0])) == 2  # the fork's own snapshots: write, render


def test_a_fork_refuses_unknown_or_unfinished_tasks(hooks_on):
    llm = ScriptedLLM(Rule(killed_at_render(Script([brief()]))))
    with pytest.raises(Exception, match="killed before rendering"):
        short_update.run(3, REPORTS, fake=llm)
    crewai_event_bus.flush()
    first = short_update.snapshots_since(0)[0][1]  # after classify: write and render hadn't finished
    with pytest.raises(ValueError, match="no task named"):
        short_update.fork(first, {"pricing_analysis": "x"}, fake=ScriptedLLM())
    with pytest.raises(ValueError, match="hadn't finished"):
        short_update.fork(first, {"write_short_brief": "x"}, fake=ScriptedLLM())


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


def test_crew_checkpoints_survive_scouts_and_memory_in_the_same_flow(world):
    """Inside the weekly Flow a crew checkpoint serializes every entity of the run. A deep recall (RecallFlow)
    or an agent's own memory save (EncodingFlow) adds one that can't be serialized, and every later checkpoint
    failed (M7, live). Scouts with their read-only, shallow memory views, then the crew, in one Flow."""
    import asyncio

    from crewai.events.types.checkpoint_events import CheckpointCompletedEvent, CheckpointFailedEvent
    from crewai.flow.flow import Flow, listen, start

    from compintel import scouting
    from compintel.memory import competitor_scope
    from tests.test_scout import ScriptedScout

    hooks.install()
    competitor_scope("ShelfMitra").remember(
        "Week 1: ShelfMitra posted Mumbai hiring.", importance=0.5, categories=["x"]
    )
    # That save ran outside any run, so its EncodingFlow stayed in crewai's runtime-state context variable
    # and the Flow below would inherit it (and fail every checkpoint). In the real Flow saves happen inside.
    crewai_event_bus.reset_runtime_state()
    outcomes: list[str] = []
    crewai_event_bus.on(CheckpointCompletedEvent)(lambda s, e: outcomes.append("ok"))
    crewai_event_bus.on(CheckpointFailedEvent)(lambda s, e: outcomes.append("failed"))

    class Week(Flow):
        @start()
        def scout(self) -> None:
            asyncio.run(
                scouting.scout_company("ShelfMitra", 2, llm=ScriptedLLM(Rule(ScriptedScout(world, 2))))
            )

        @listen(scout)
        def write(self) -> None:
            short_update.run(3, REPORTS, fake=ScriptedLLM(Rule(Script([brief()]))))

    Week().kickoff()
    crewai_event_bus.flush()
    assert outcomes == ["ok", "ok", "ok"]


def test_the_what_if_script_writes_its_comparison(hooks_on, tmp_path, monkeypatch):
    """scripts/what_if.py end to end on the scripted crew: the doc is written, the fork read the edit."""
    import importlib.util

    from compintel import config
    from evals import data

    path = Path(__file__).parents[1] / "scripts" / "what_if.py"
    spec = importlib.util.spec_from_file_location("what_if", path)
    assert spec and spec.loader
    what_if = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(what_if)

    (tmp_path / "docs").mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(what_if, "WEEK", 3)  # the fixtures are week 3's
    monkeypatch.setattr(
        data,
        "load_weeks",
        lambda weeks: [data.WeekRun(3, "s", REPORTS, Brief.model_validate(brief()), "routine")],
    )
    real_run, real_fork = short_update.run, short_update.fork
    script = Script([brief(), brief(headline="StockSathi's 50% cut")])
    monkeypatch.setattr(
        short_update, "run", lambda week, reports: real_run(week, reports, fake=ScriptedLLM(Rule(script)))
    )
    monkeypatch.setattr(
        short_update, "fork", lambda loc, ov: real_fork(loc, ov, fake=ScriptedLLM(Rule(script)))
    )
    what_if.main()
    doc = (tmp_path / "docs" / "what-if.md").read_text()
    assert "## Did the brief follow the analysis or the evidence?" in doc and "```diff" in doc
    assert "StockSathi's 50% cut" in doc  # the forked brief's headline
    assert "WHAT-IF: StockSathi cut Pro by 50%" in script.writer_prompts[-1]
