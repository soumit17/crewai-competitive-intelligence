"""M6 human feedback: the @human_feedback review (all three outcomes, the bounded revise loop, learned
lessons), the async provider (pause, resume in this process and in another), and the two other HITL patterns
(a crew task's human_input, a hook's request_human_input). All $0: scripted reviewer, model and embedder."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from compintel import hooks, review
from compintel.crews import short_update
from compintel.flow import MAX_REVISIONS, is_paused, read_state_index, run_week
from compintel.memory import hitl_lessons
from tests import hitl
from tests.fakes import Rule, ScriptedLLM
from tests.test_flow import stubs  # noqa: F401 - the fixture: scouting and the two crews stubbed
from tests.test_short_update_crew import REPORTS, Script, brief

KOLKATA = ("Always lead with the single biggest threat to Kolkata.", "revise")


@pytest.fixture
def revisions(monkeypatch):
    """RevisionCrew stubbed (it has its own test below): each revision marks the headline."""
    from compintel.crews import revision

    calls = []

    def revise(brief, feedback, lessons, signals, human_input=False, fake=None):
        calls.append({"feedback": feedback, "lessons": lessons})
        return brief.model_copy(update={"headline": f"{brief.headline} (rev {len(calls)})"})

    monkeypatch.setattr(revision, "run", revise)
    return calls


def review_with(monkeypatch, *answers):
    reviewer = hitl.ScriptedReviewer(*answers)
    llm = hitl.install(monkeypatch, reviewer)
    return reviewer, llm


def audit(week):
    return json.loads(Path(f"briefs/week-{week:02d}.feedback.json").read_text())["reviews"]


def test_approve_publishes_what_the_reviewer_saw(world, stubs, monkeypatch):  # noqa: F811
    reviewer, _ = review_with(monkeypatch, ("Looks right, ship it.", "approve"))
    flow = run_week(1)
    assert flow.state.published_path == "briefs/week-01.md"
    assert [r["outcome"] for r in audit(1)] == ["approve"]
    assert audit(1)[0]["metadata"] == {"stage": "editorial"}
    assert len(reviewer.shown) == 1
    assert Path("briefs/drafts/week-01.crew.md").exists()


def test_revise_loops_back_to_review_then_publishes(world, stubs, revisions, monkeypatch):  # noqa: F811
    reviewer, _ = review_with(monkeypatch, ("Name the Kolkata office in the headline.", "revise"))
    flow = run_week(1)
    assert [r["outcome"] for r in audit(1)] == ["revise", "approve"]
    assert revisions[0]["feedback"] == "Name the Kolkata office in the headline."
    assert reviewer.shown[1].headline.endswith("(rev 1)")  # the second review saw the revision
    assert flow.state.brief is not None and flow.state.brief.headline.endswith("(rev 1)")
    assert flow.state.revision_count == 1


def test_the_revise_loop_is_bounded(world, stubs, revisions, monkeypatch):  # noqa: F811
    asks = [(f"Change {n}.", "revise") for n in range(1, 4)]
    review_with(monkeypatch, *asks)
    flow = run_week(1)
    assert len(revisions) == MAX_REVISIONS  # the third "revise" did not run the crew
    assert [r["outcome"] for r in audit(1)] == ["revise"] * 3
    assert read_state_index()["published"]["1"] == flow.state.id
    published = Path("briefs/week-01.md").read_text()
    assert "Editor's note" in published and "Change 3." in published


def test_reject_archives_and_publishes_nothing(world, stubs, monkeypatch):  # noqa: F811
    review_with(monkeypatch, ("This is wrong, bin it.", "reject"))
    run_week(1)
    assert Path("briefs/rejected/week-01.md").exists() and not Path("briefs/week-01.md").exists()
    assert read_state_index() == {"published": {}, "in_progress": {}}
    with pytest.raises(ValueError, match="no published state for week 1"):
        run_week(2)  # a rejected week can't be forked: rerun it


def test_empty_feedback_is_not_approval(world, stubs, revisions, monkeypatch):  # noqa: F811
    _, llm = review_with(monkeypatch, ("", "revise"))
    run_week(1)
    assert [r["outcome"] for r in audit(1)] == ["revise", "approve"]  # default_outcome, no model call for it
    collapses = [c for c in llm.calls if getattr(c["response_model"], "__name__", "") == "FeedbackOutcome"]
    assert len(collapses) == 1


def test_a_lesson_from_week_one_is_applied_before_week_two_is_reviewed(
    world,
    stubs,  # noqa: F811
    revisions,
    monkeypatch,
):
    review_with(monkeypatch, KOLKATA)
    week1 = run_week(1)
    assert hitl_lessons(week1.memory, "brief threats") == [KOLKATA[0]]  # distilled and stored, source hitl

    reviewer, _ = review_with(monkeypatch, ("Good.", "approve"))
    week2 = run_week(2)
    crew_draft = Path("briefs/drafts/week-02.crew.md").read_text()
    assert not crew_draft.startswith("# Week 02: Kolkata")  # the crew didn't write it that way...
    assert reviewer.shown[0].headline.startswith("Kolkata:")  # ...the pre-review did, before the human saw it
    assert audit(2)[0]["changed_by_lessons"] is True
    assert week2.state.brief is not None and week2.state.brief.headline.startswith("Kolkata:")  # published
    assert Path("briefs/week-02.md").read_text().startswith("# Week 02: Kolkata:")


def test_async_review_pauses_and_resumes_with_a_dict_output(world, stubs, monkeypatch):  # noqa: F811
    review_with(monkeypatch)
    flow = run_week(1, reviewer=review.PendingReview())
    assert is_paused(flow) and not Path("briefs/week-01.md").exists()
    [pending] = review.list_pending()
    assert pending["flow_id"] == flow.state.id and pending["markdown"].startswith("# Week 01:")
    assert read_state_index()["in_progress"]["1"] == flow.state.id

    resumed, _ = review.resume(flow.state.id, "Approved, looks good.")
    # The reviewed output came back from JSON as a dict; publish turned it back into a Brief.
    assert resumed.last_human_feedback is not None and isinstance(resumed.last_human_feedback.output, dict)
    assert resumed.state.id == flow.state.id and resumed.state.published_path == "briefs/week-01.md"
    assert review.list_pending() == [] and read_state_index()["published"]["1"] == flow.state.id


def test_async_revise_pauses_again(world, stubs, revisions, monkeypatch):  # noqa: F811
    review_with(monkeypatch, ("Shorter headline.", "revise"))
    flow = run_week(1, reviewer=review.PendingReview())
    resumed, _ = review.resume(flow.state.id, "Shorter headline.")
    assert len(revisions) == 1 and is_paused(resumed)  # revised, then waiting for the next review
    review.resume(flow.state.id, "Approved, looks good.")
    assert [r["outcome"] for r in audit(1)] == ["revise", "approve"]


def test_async_review_resumes_in_another_process(world, stubs, monkeypatch, tmp_path):  # noqa: F811
    review_with(monkeypatch)
    flow = run_week(1, reviewer=review.PendingReview())
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root)}
    done = subprocess.run(
        [sys.executable, "-m", "tests.resume_review", flow.state.id, "Approved, looks good."],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result == {"state_id": flow.state.id, "published": "briefs/week-01.md"}
    assert read_state_index()["published"]["1"] == flow.state.id and review.list_pending() == []
    assert [r["outcome"] for r in audit(1)] == ["approve"]


def test_revision_crew_applies_feedback_and_asks_again_with_human_input(monkeypatch):
    from compintel.crews import revision
    from compintel.models import Brief

    hooks.install()
    replies = iter(["Also mention the Series B.", ""])  # one more round on the console, then Enter
    monkeypatch.setattr("builtins.input", lambda *_: next(replies))
    llm = ScriptedLLM(Rule(brief(headline="Revised once")), Rule(brief(headline="Revised twice")))
    llm._rules[0].times = 1
    revised = revision.run(
        Brief.model_validate(brief()),
        "Lead with ShelfMitra.",
        ["Always lead with the single biggest threat to Kolkata."],
        short_update.signals_input(REPORTS),
        human_input=True,
        fake=llm,
    )
    assert revised.headline == "Revised twice"
    prompts = [json.dumps(c["messages"]) for c in llm.calls if c["role"] == "Editor"]
    assert "Lead with ShelfMitra." in prompts[0] and "single biggest threat to Kolkata" in prompts[0]
    assert "Also mention the Series B." in prompts[-1]  # crewai fed the console feedback back to the Editor


def test_render_gate_hook_refusal_blocks_the_render_not_the_crew(monkeypatch):
    hooks.install()
    hooks.install_render_gate()
    monkeypatch.setattr("builtins.input", lambda *_: "no")
    result = short_update.run(3, REPORTS, fake=ScriptedLLM(Rule(Script([brief()]))))
    # render_brief is result_as_answer: the block message became the crew's answer (and the draft .md).
    assert hooks.render_refused(result.markdown)
    assert hooks.render_refused(Path("briefs/drafts/week-03.md").read_text())
    assert result.brief.headline  # the approved Brief JSON itself is intact


def test_render_gate_hook_lets_an_approved_render_through(monkeypatch):
    hooks.install()
    hooks.install_render_gate()
    monkeypatch.setattr("builtins.input", lambda *_: "")
    result = short_update.run(3, REPORTS, fake=ScriptedLLM(Rule(Script([brief()]))))
    assert result.markdown.startswith("# Week 03:")
