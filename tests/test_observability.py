"""M8 hooks (iteration guard, redaction, step tracer, input defaults, the deep dive's crew-scoped hook) and
the RunMonitor / live console. Unit cases first, then each hook inside a real (scripted) crewai run. $0."""

import io
import json
from types import SimpleNamespace

import pytest
from crewai.events.event_bus import crewai_event_bus
from crewai.hooks import HookAborted, InterceptionPoint
from crewai.hooks.dispatch import get_hooks
from rich.console import Console

from compintel import hello, hooks, listeners
from compintel.cost import get_ledger
from compintel.crews import deep_dive, short_update
from compintel.crews.deep_dive import DeepDiveCrew
from tests.deep_dive_script import WEEK2_REPORTS, DeepDiveScript
from tests.fakes import Rule, ScriptedLLM
from tests.test_flow import brief_for
from tests.test_flow import stubs as stubs  # the Flow fixture with crews stubbed
from tests.test_short_update_crew import REPORTS, Script, brief

TAKE = {"company": "StockSathi", "kind": "pricing_change", "impact_on_us": "high", "why": "Undercuts us."}
LEAKED = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"


# ---------------------------------------------------------------- iteration_guard
def test_iteration_guard_allows_twelve_and_stops_the_thirteenth():
    hooks.iteration_guard(SimpleNamespace(iterations=12, agent=None))
    with pytest.raises(HookAborted, match="iteration 13"):
        hooks.iteration_guard(SimpleNamespace(iterations=13, agent=SimpleNamespace(role="Crew Manager")))


def test_iteration_guard_ignores_direct_calls():
    hooks.iteration_guard(SimpleNamespace())  # router / memory / knowledge calls carry no iteration count


# ---------------------------------------------------------------- redact_secrets
@pytest.mark.parametrize(
    "secret",
    [
        LEAKED,
        "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX",
        "AKIAABCDEFGHIJKLMNOP",
        "Bearer abcdefghijklmnopqrstuvwxyz123",
    ],
)
def test_redaction_replaces_key_shapes(secret):
    out = hooks.redact_secrets(SimpleNamespace(response=f"the key is {secret}, keep it safe"))
    assert out == f"the key is {hooks.REDACTED}, keep it safe"


def test_redaction_replaces_our_own_key_whatever_its_shape(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "plain-looking-secret-value")
    out = hooks.redact_secrets(SimpleNamespace(response="echo plain-looking-secret-value back"))
    assert out == f"echo {hooks.REDACTED} back"


def test_clean_responses_are_left_alone():
    # None, not the same string: the dispatch is then recorded as "proceeded", not "modified".
    assert hooks.redact_secrets(SimpleNamespace(response="ShelfMitra raised a Series B.")) is None
    assert hooks.redact_secrets(SimpleNamespace(response=None)) is None


def test_a_leaked_key_never_reaches_the_typed_output():
    """POST_MODEL_CALL replace semantics on a structured answer: crewai hands the hook the JSON and
    re-validates the redacted text into the model."""
    hooks.install()
    monitor = listeners.RunMonitor()
    out = hello.run(llm=ScriptedLLM(Rule({**TAKE, "why": f"Their admin page leaked {LEAKED}."})))
    crewai_event_bus.flush()
    assert out.pydantic.why == f"Their admin page leaked {hooks.REDACTED}."
    assert monitor.hooks[("post_model_call", "modified")] == 1


# ---------------------------------------------------------------- step_tracer
def test_every_task_is_timed_and_its_output_left_untouched():
    hooks.install()
    result = short_update.run(3, REPORTS, fake=ScriptedLLM(Rule(Script([brief()]))))
    assert result.brief.week == 3  # POST_STEP returning None keeps the task output
    rows = [json.loads(line) for line in (get_ledger().path.parent / "steps.jsonl").read_text().splitlines()]
    tasks = [r for r in rows if r["kind"] == "task"]
    assert [r["step"] for r in tasks] == ["classify_signals", "write_short_brief", "render_draft"]
    assert [r["agent"] for r in tasks] == ["Market Analyst", "Brief Writer", "Brief Writer"]
    assert all(r["seconds"] is not None and r["seconds"] >= 0 for r in tasks)


def test_flow_methods_are_timed_too(world, stubs):
    from compintel.flow import run_week

    hooks.install()
    run_week(1)
    steps = [e["step"] for e in hooks.step_trace.entries if e["kind"] == "flow_method"]
    assert {"load_week", "scout_all", "assess_materiality", "run_short_update", "publish"} <= set(steps)
    assert all(e["flow_id"] for e in hooks.step_trace.entries if e["kind"] == "flow_method")


# ---------------------------------------------------------------- inputs_defaults
def _input_ctx(inputs, crew=None):
    return SimpleNamespace(inputs=inputs, crew=crew)


def test_crews_get_a_currency_default_and_flows_do_not():
    crew_inputs, flow_inputs = {"week": 2}, {"week": 2}
    hooks.inputs_defaults(_input_ctx(crew_inputs, crew=SimpleNamespace(checkpoint_kickoff_event_id=None)))
    hooks.inputs_defaults(_input_ctx(flow_inputs))
    assert crew_inputs == {"week": 2, "currency": "INR"}
    assert flow_inputs == {"week": 2}  # a Flow's inputs become fields of its typed state


@pytest.mark.parametrize("week", [0, 4, "two", None])
def test_weeks_outside_the_world_are_refused(week):
    if week is None:
        hooks.inputs_defaults(_input_ctx({}))  # no week (a chat session): nothing to check
        return
    with pytest.raises(HookAborted, match="outside the mock world"):
        hooks.inputs_defaults(_input_ctx({"week": week}))


def test_a_resuming_crew_is_left_alone():
    """Adding a key to a resume's empty inputs makes crewai re-interpolate templates without `week`."""
    inputs: dict = {}
    hooks.inputs_defaults(_input_ctx(inputs, crew=SimpleNamespace(checkpoint_kickoff_event_id="evt")))
    assert inputs == {}


def test_a_bad_week_stops_the_crew_before_any_model_call():
    hooks.install()
    llm = ScriptedLLM(Rule(Script([brief()])))
    with pytest.raises(HookAborted, match="week 4"):
        short_update.run(4, REPORTS, fake=llm)
    assert llm.calls == []


# ---------------------------------------------------------------- budget_gate at EXECUTION_START
def test_a_crew_over_the_cap_does_not_start():
    get_ledger().record("claude-haiku-5-5", {"input_tokens": 200_000_000}, response_id="big")  # $20 > $12
    hooks.install()
    monitor = listeners.RunMonitor()
    llm = ScriptedLLM(Rule(Script([brief()])))
    with pytest.raises(HookAborted, match="spend cap"):
        short_update.run(3, REPORTS, fake=llm)
    crewai_event_bus.flush()
    assert llm.calls == []
    assert monitor.hooks[("execution_start", "aborted")] == 1
    assert monitor.hook_aborts == ["budget-gate: P3 spend cap reached: $20.0000 of $12.00"]


# ---------------------------------------------------------------- the deep dive's crew-scoped hook
@pytest.fixture
def on(world):
    hooks.install()
    return world


def _scoped_hooks():
    return [h for h in get_hooks(InterceptionPoint.PRE_MODEL_CALL) if "validate_deep_dive_inputs" in repr(h)]


def test_deep_dive_without_signals_is_stopped_before_any_analysis(on):
    llm = ScriptedLLM(Rule(DeepDiveScript()))
    with pytest.raises(HookAborted, match="no signals to analyse"):
        deep_dive.run(2, {}, variant="sequential", fake=llm)
    # Only crewai's planner ran (its agent isn't in the crew, so the hook doesn't see it). None of the
    # specialists did, not even the two analyses that start in parallel with the one that hit the hook first.
    specialists = {"Pricing Analyst", "Product Analyst", "Market Analyst", "Strategist", "Brief Writer"}
    assert not specialists & {c["role"] for c in llm.calls}


def test_the_crew_scoped_hook_leaves_other_crews_alone(on):
    DeepDiveCrew(variant="sequential")  # registers its hook globally, as crewai does
    assert len(_scoped_hooks()) == 1
    # A ShortUpdateCrew in the same process: the hook sees calls that aren't its crew's, and passes them.
    result = short_update.run(3, REPORTS, fake=ScriptedLLM(Rule(Script([brief()]))))
    assert result.brief.week == 3


def test_a_finished_deep_dive_unregisters_its_hook(on):
    deep_dive.run(2, WEEK2_REPORTS, variant="sequential", fake=ScriptedLLM(Rule(DeepDiveScript())))
    assert _scoped_hooks() == []


# ---------------------------------------------------------------- RunMonitor and the live console
def test_the_monitor_sees_every_event_family_of_a_week(world, stubs, monkeypatch):
    """With the real console review: crewai's ConsoleProvider emits the human-feedback events (the Flow
    doesn't when a provider is installed)."""
    from compintel import review
    from compintel.flow import run_week
    from tests import hitl

    hitl.install(monkeypatch, hitl.ScriptedReviewer(("Looks good, ship it.", "approve")))
    monkeypatch.setattr("builtins.input", lambda *_: "Looks good, ship it.")
    hooks.install()
    monitor = listeners.RunMonitor()
    run_week(1, reviewer=review.ConsoleReview())
    crewai_event_bus.flush()
    c = monitor.counts
    assert c["flow_methods_started"] == c["flow_methods_finished"] >= 6
    assert c["feedback_requested"] == c["feedback_received"] == 1
    assert c["memory_saves"] >= 1  # publish remembers the week's signals and brief facts
    assert "publish" in monitor.finished_methods and monitor.running_methods == []
    points = {point for point, _ in monitor.hooks}
    assert {"execution_start", "input", "pre_step", "post_step"} <= points
    assert any("review answered" in line for line in monitor.timeline)


def test_the_monitor_sees_tasks_tools_guardrails_and_llm_calls():
    hooks.install()
    monitor = listeners.RunMonitor()
    short_update.run(3, REPORTS, fake=ScriptedLLM(Rule(Script([brief()]))))
    crewai_event_bus.flush()
    c = monitor.counts
    assert c["tasks_started"] == c["tasks_completed"] == 3
    assert c["tool_calls"] >= 1  # render_brief
    assert c["guardrails_passed"] == 4  # classify: threat levels; write: rumor (LLM), citations, credibility
    assert c["llm_calls"] >= 4
    assert monitor.active_tasks == {}


def test_live_console_draws_the_panel_and_prints_lines_above_it():
    out = io.StringIO()
    monitor = listeners.RunMonitor()
    with listeners.LiveConsole(monitor, Console(file=out, force_terminal=True, width=120)) as live:
        monitor.on_line("[green]✓ step[/green] load_week")
        with listeners.live_paused():  # what the console review does around input()
            assert not live._live.is_started
        assert live._live.is_started
        live._live.refresh()
    text = out.getvalue()
    assert "load_week" in text and "hooks" in text and "P3 $" in text
    assert listeners._active_live is None


def test_live_paused_is_harmless_without_a_live_console():
    with listeners.live_paused():
        pass


def review_context():
    from compintel import review

    return review.PendingFeedbackContext(
        flow_id="f",
        flow_class="CompetitiveIntelFlow",
        method_name="editorial_review",
        method_output=brief_for(1, "routine"),
        message="Review",
    )


FLOW = SimpleNamespace(name="CompetitiveIntelFlow")


def test_hitl_review_pauses_the_live_panel(world, stubs, monkeypatch):
    """ConsoleReview takes the panel down around its input() prompt."""
    from compintel import review

    seen = {}

    def fake_input(*_):
        seen["paused"] = listeners._active_live is not None and not listeners._active_live._live.is_started
        return "Looks good, approve."

    monkeypatch.setattr("builtins.input", fake_input)
    monitor = listeners.RunMonitor()
    with listeners.LiveConsole(monitor, Console(file=io.StringIO(), force_terminal=True)):
        assert review.ConsoleReview().request_feedback(review_context(), flow=FLOW) == "Looks good, approve."
    assert seen["paused"] is True


# ---------------------------------------------------------------- console review input
@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("\x1b[6~", ""),  # PageDown
        ("\x1b[A\x1b[B", ""),  # arrows
        ("Looks good\x1b[D, ship it", "Looks good, ship it"),
        ("  lead with Kolkata  ", "lead with Kolkata"),
    ],
)
def test_terminal_key_sequences_are_not_feedback(raw, clean):
    from compintel.review import clean_feedback

    assert clean_feedback(raw) == clean


def test_a_review_answer_of_only_key_presses_is_asked_again(monkeypatch):
    """Live M8a: PageDown pressed to scroll the brief became the feedback, and was routed as "revise"."""
    from compintel import review

    answers = iter(["\x1b[6~", "\x1b[6~", "Looks good, approve."])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    assert review.ConsoleReview().request_feedback(review_context(), flow=FLOW) == "Looks good, approve."


def test_an_empty_answer_is_asked_once_more_and_two_mean_revise(monkeypatch, capsys):
    """Live M8b: crewai's prompt said "Enter to skip"; six Enters were six instruction-less revisions."""
    from compintel import review

    calls = []
    monkeypatch.setattr("builtins.input", lambda *_: calls.append(1) or "")
    assert review.ConsoleReview().request_feedback(review_context(), flow=FLOW) == ""
    assert len(calls) == 2
    out = capsys.readouterr().out
    assert out.count(review.ConsoleReview.EMPTY_HINT) == 1 and "Enter alone = revise" in out
    assert "Press Enter to skip" not in out


def test_the_answer_after_an_empty_one_is_the_one_recorded(monkeypatch):
    """The received event (what the trace and the monitor see) carries the final answer, sent once."""
    from crewai.events.types.flow_events import HumanFeedbackReceivedEvent

    from compintel import review

    received = []
    crewai_event_bus.on(HumanFeedbackReceivedEvent)(lambda _, e: received.append(e.feedback))
    answers = iter(["", "approve"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    assert review.ConsoleReview().request_feedback(review_context(), flow=FLOW) == "approve"
    crewai_event_bus.flush()
    assert received == ["approve"]
