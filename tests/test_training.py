"""M8d: `crewai test` / `crewai train` on the ShortUpdateCrew (compintel.training), scripted, $0."""

from __future__ import annotations

import json
import pickle
import re

import pytest
from crewai.events.event_bus import crewai_event_bus
from crewai.events.types.crew_events import CrewTestResultEvent

from compintel import training
from compintel.crews import short_update
from tests.fakes import Rule, ScriptedLLM
from tests.test_short_update_crew import REPORTS, Script, brief

FEEDBACK = {
    "classify_signals": "An unchanged price is not high impact.",
    "write_short_brief": "Never call a price the lowest unless you checked every competitor.",
    "render_draft": "Render it as it is.",
}
SUGGESTIONS = {"Market Analyst": "Rate repeats low.", "Brief Writer": "Check every competitor first."}


@pytest.fixture
def week3(monkeypatch):
    monkeypatch.setattr(training, "week_inputs", lambda week=None: (3, REPORTS))


def crew_llm(script: Script) -> ScriptedLLM:
    """The crew's answers, plus the training evaluator's: each agent's own LLM turns the feedback it got into
    suggestions."""

    def answer(messages, response_model):
        if getattr(response_model, "__name__", "") == "TrainingTaskEvaluation":
            assert "human feedback" in json.dumps(messages).lower()
            return {"suggestions": ["Follow the feedback."], "quality": 7.0, "final_summary": "ok"}
        return script(messages, response_model)

    return ScriptedLLM(Rule(answer))


def test_crewai_test_scores_every_task_with_the_llm_we_give_it(week3):
    results = []
    crewai_event_bus.on(CrewTestResultEvent)(lambda _, e: results.append((e.quality, e.model)))
    judge = ScriptedLLM(Rule(lambda m, rm: {"quality": 8.0}))
    training.run_test(1, "anthropic/claude-haiku-5-5", fake=crew_llm(Script([brief()])), eval_llm=judge)
    crewai_event_bus.flush()
    assert len(judge.calls) == 3  # one evaluation per task
    assert [q for q, _ in results] == [8.0, 8.0, 8.0]


def test_the_model_string_from_the_cli_becomes_our_native_anthropic_llm():
    from compintel.config import make_llm

    llm = make_llm(model="anthropic/claude-sonnet-5-5")
    assert llm.model == "claude-sonnet-5-5" and type(llm).__name__ == "AnthropicCompletion"


def test_training_feeds_each_task_its_feedback_and_saves_suggestions_by_role(week3, tmp_path):
    out = tmp_path / "training" / "short_update.pkl"
    script = Script([brief(), brief(), brief()])
    training.run_train(1, str(out), fake=crew_llm(script), feedback=FEEDBACK)
    trained = pickle.loads(out.read_bytes())
    assert set(trained) == {"Market Analyst", "Brief Writer"}  # by role, as agents look it up
    assert all(trained[r]["suggestions"] for r in trained)
    # The feedback reached the agent: its redo saw it.
    assert any("Never call a price the lowest" in p for p in script.writer_prompts)


def test_a_trained_file_is_used_only_when_named(hooks_on, tmp_path):
    trained = tmp_path / "short_update.pkl"
    data = {r: {"suggestions": [s], "quality": 7.0, "final_summary": "x"} for r, s in SUGGESTIONS.items()}
    trained.write_bytes(pickle.dumps(data))
    with_file = Script([brief()])
    short_update.run(3, REPORTS, fake=ScriptedLLM(Rule(with_file)), trained_agents_file=str(trained))
    assert "You MUST follow these instructions" in with_file.writer_prompts[0]
    assert "Check every competitor first." in with_file.writer_prompts[0]
    without = Script([brief()])
    short_update.run(3, REPORTS, fake=ScriptedLLM(Rule(without)))
    assert "Check every competitor first." not in without.writer_prompts[0]


def test_crewais_default_trained_file_is_refused():
    """Every agent run reads trained_agents_data.pkl by default: training into it would leak into every
    crew."""
    with pytest.raises(SystemExit, match="refusing trained_agents_data.pkl"):
        training.run_train(1, "trained_agents_data.pkl", feedback=FEEDBACK)


def test_the_reviewed_feedback_file_covers_every_task(world, hooks_on):
    feedback = training.load_feedback()
    crew = short_update.load(fake=ScriptedLLM())
    assert set(feedback) == {t.name for t in crew.tasks}


@pytest.fixture
def hooks_on():
    from compintel import hooks

    hooks.install()


def test_the_comparison_script_scores_both_briefs(hooks_on, tmp_path, monkeypatch):
    """scripts/compare_trained.py end to end, scripted: two crew runs (the second with the file), both
    judged."""
    import importlib.util
    from pathlib import Path

    from compintel import config
    from compintel.models import Brief
    from evals import data, quality

    path = Path(__file__).parents[1] / "scripts" / "compare_trained.py"
    spec = importlib.util.spec_from_file_location("compare_trained", path)
    assert spec and spec.loader
    compare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(compare)

    trained = tmp_path / "short_update.pkl"
    trained.write_bytes(
        pickle.dumps({r: {"suggestions": [s], "quality": 7.0} for r, s in SUGGESTIONS.items()})
    )
    (tmp_path / "docs").mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        data,
        "load_weeks",
        lambda weeks: [data.WeekRun(3, "s", REPORTS, Brief.model_validate(brief()), "routine")],
    )
    scripts = []
    real_run = short_update.run

    def run(week, reports, trained_agents_file=None):
        scripts.append(Script([brief()]))
        return real_run(
            week, reports, fake=ScriptedLLM(Rule(scripts[-1])), trained_agents_file=trained_agents_file
        )

    def judge(messages, response_model):
        if response_model.__name__ == "FaithfulnessVerdicts":  # claim numbers skip claims citing nothing
            numbers = [int(n) for n in re.findall(r"Claim (\d+) \(", messages[-1]["content"])]
            return {"verdicts": [{"n": n, "verdict": "supported", "reason": "ok"} for n in numbers]}
        return dict.fromkeys(quality.CRITERIA, 4) | {"comment": "ok"}

    monkeypatch.setattr(short_update, "run", run)
    monkeypatch.setattr(config, "make_llm", lambda **_: ScriptedLLM(Rule(judge)))
    compare.main(str(trained))
    doc = (tmp_path / "docs" / "training.md").read_text()
    assert "Check every competitor first." in doc and "| Faithfulness (judge) | 100.0 | 100.0 |" in doc
    assert "Check every competitor first." not in scripts[0].writer_prompts[0]  # untrained run
    assert "Check every competitor first." in scripts[1].writer_prompts[0]  # trained run
