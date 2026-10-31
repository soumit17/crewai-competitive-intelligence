"""M3 ShortUpdateCrew: the JSON-first definition, a full scripted run, and both guardrail retry paths."""

import json
from pathlib import Path

import pytest
from crewai import Process
from crewai.llms.providers.anthropic.completion import AnthropicCompletion

from compintel import hooks
from compintel.config import AGENT_LIMITS
from compintel.crews import short_update
from compintel.models import Brief, Signal, SignalReport, ThreatAssessments
from compintel.tools.render_brief import render_markdown
from tests.fakes import Rule, ScriptedLLM, ToolCall, tool_results

COMPANIES = ("StockSathi", "ShelfMitra", "BahiBook")


def signal(sid, kind, detail, cred=0.9, impact="low"):
    return Signal(
        id=sid,
        company=sid.split("-")[0],
        kind=kind,
        detail=detail,
        source_ref="x",
        source_credibility=cred,
        novelty="new",
        impact_on_us=impact,
    )


REPORTS = {
    "StockSathi": SignalReport(
        company="StockSathi",
        week=3,
        summary="",
        signals=[
            signal("StockSathi-3-1", "rating_change", "App rating fell from 4.3 to 3.9", impact="medium"),
            signal("StockSathi-3-2", "rumor", "Blog claims StockSathi is being acquired", cred=0.2),
        ],
    ),
    "BahiBook": SignalReport(
        company="BahiBook",
        week=3,
        summary="",
        signals=[
            signal("BahiBook-3-1", "hiring_expansion", "Hiring a PM for GST e-invoicing", impact="medium")
        ],
    ),
}

THREATS = {
    "threats": [
        {"company": "StockSathi", "threat": "medium", "rationale": "Rating dip [StockSathi-3-1]", "signal_ids": ["StockSathi-3-1"]},
        {"company": "ShelfMitra", "threat": "low", "rationale": "Quiet week [BahiBook-3-1]", "signal_ids": ["BahiBook-3-1"]},
        {"company": "BahiBook", "threat": "medium", "rationale": "GST e-invoicing PM [BahiBook-3-1]", "signal_ids": ["BahiBook-3-1"]},
    ]
}  # fmt: skip


def brief(**changes):
    return {
        "week": 3,
        "headline": "BahiBook eyes GST e-invoicing",
        "tl_dr": ["BahiBook is hiring a GST e-invoicing PM [BahiBook-3-1]", "StockSathi rating fell to 3.9 [StockSathi-3-1]"],
        "since_last_week": ["First brief: no earlier week to compare."],
        "threats": THREATS["threats"],
        "recommendations": [{"action": "Pull e-invoicing forward", "owner": "Product", "urgency": "this_month", "signal_ids": ["BahiBook-3-1"]}],
        "watchlist": ["StockSathi reviews [StockSathi-3-1]"],
        "discounted_claims": ["Unverified acquisition rumor [StockSathi-3-2]"],
        "route": "routine",
        **changes,
    }  # fmt: skip


class Script:
    """The crew's model decisions, keyed by agent role. Writer answers come from a queue (for retries)."""

    def __init__(self, writer_answers, guardrail_answers=None):
        self.writer_answers = list(writer_answers)
        self.guardrail_answers = list(guardrail_answers or [{"valid": True, "feedback": None}])
        self.writer_prompts: list[str] = []

    def __call__(self, messages, response_model):
        prompt = json.dumps(messages, default=str)
        if response_model is not None and response_model.__name__ == "LLMGuardrailResult":
            return (
                self.guardrail_answers.pop(0)
                if len(self.guardrail_answers) > 1
                else self.guardrail_answers[0]
            )
        if response_model is ThreatAssessments:
            return THREATS
        if "Call render_brief" in prompt:
            if '"role": "tool"' not in tool_results(messages):
                return [ToolCall("render_brief", {"week": 3})]
            return "(result_as_answer should have ended the loop before this call)"
        self.writer_prompts.append(prompt)
        return json.dumps(self.writer_answers.pop(0))


@pytest.fixture
def hooks_on():
    hooks.install()


def run(script, **kwargs):
    llm = ScriptedLLM(Rule(script))
    return short_update.run(3, REPORTS, fake=llm, **kwargs), llm


def test_crew_is_defined_in_json(hooks_on):
    crew = short_update.load()
    assert crew.process == Process.sequential
    assert [a.role for a in crew.agents] == ["Market Analyst", "Brief Writer"]
    assert [t.name for t in crew.tasks] == ["classify_signals", "write_short_brief", "render_draft"]
    classify, write, render = crew.tasks
    assert classify.output_pydantic is ThreatAssessments and write.output_pydantic is Brief
    assert write.context == [classify]
    assert len(write.guardrails) == 3 and write.guardrail_max_retries == 2  # type: ignore[arg-type]
    assert write.callback is short_update.save_draft
    assert [t.name for t in render.tools or []] == ["render_brief"] and render.tools[0].result_as_answer  # type: ignore[index]
    for agent in crew.agents:
        for name, value in AGENT_LIMITS.items():
            assert getattr(agent, name) == value, (agent.role, name)


def test_models_come_from_config_and_final_only_moves_the_writer(hooks_on):
    dev = short_update.load()
    final = short_update.load(final=True)
    assert all(isinstance(a.llm, AnthropicCompletion) for a in dev.agents + final.agents)
    assert [a.llm.model for a in dev.agents] == ["claude-haiku-5-5", "claude-haiku-5-5"]  # type: ignore[union-attr]
    assert [a.llm.model for a in final.agents] == ["claude-haiku-5-5", "claude-sonnet-5-5"]  # type: ignore[union-attr]


def test_full_run_produces_a_valid_brief_and_both_files(hooks_on):
    script = Script([brief()])
    result, llm = run(script)
    assert result.brief.route == "routine" and len(result.threats.threats) == 3
    # context: the writer saw the analyst's threat assessments
    assert "GST e-invoicing PM [BahiBook-3-1]" in script.writer_prompts[0]
    # result_as_answer: the crew's final output is the tool's markdown, byte for byte
    assert result.markdown == render_markdown(result.brief)
    assert Path("briefs/drafts/week-03.md").read_text() == result.markdown  # output_file
    assert (
        Brief.model_validate_json(Path("briefs/drafts/week-03.json").read_text()) == result.brief
    )  # callback
    roles = [c["role"] for c in llm.calls]
    assert roles == ["Market Analyst", "Brief Writer", "Guardrail Agent", "Brief Writer"]


def test_function_guardrail_failure_retries_with_feedback(hooks_on):
    uncited = brief(tl_dr=["BahiBook is hiring a PM"])
    script = Script([uncited, brief()])
    result, llm = run(script)
    assert result.brief.tl_dr[0].endswith("[BahiBook-3-1]")
    assert len(script.writer_prompts) == 2
    assert "tl_dr[0] cites no signal id" in script.writer_prompts[1]  # the guardrail's error, fed back
    # The LLM rule (first) judged the first draft only; the function's retry is re-checked by that function.
    assert [c["role"] for c in llm.calls].count("Guardrail Agent") == 1


def test_low_credibility_signal_used_as_fact_is_caught_for_free(hooks_on):
    rumor_as_fact = brief(tl_dr=["StockSathi is being acquired [StockSathi-3-2]"], discounted_claims=[])
    script = Script([rumor_as_fact, brief()])
    result, llm = run(script)
    assert result.brief.discounted_claims == ["Unverified acquisition rumor [StockSathi-3-2]"]
    assert "StockSathi-3-2 (credibility < 0.5) is missing from discounted_claims" in script.writer_prompts[1]
    assert "StockSathi-3-2 (credibility < 0.5) is used as fact" in script.writer_prompts[1]
    assert [c["role"] for c in llm.calls].count("Guardrail Agent") == 1


def test_string_guardrail_judges_wording_the_functions_cant(hooks_on):
    # Credible signal, speculative wording: only reading the text can catch this.
    speculative = brief(tl_dr=["Rumored: StockSathi will cut prices again next week [StockSathi-3-1]"])
    script = Script(
        [speculative, brief()],
        guardrail_answers=[
            {"valid": False, "feedback": "A rumored claim is stated in the TL;DR."},
            {"valid": True},
        ],
    )
    result, llm = run(script)
    assert "Rumored" not in result.brief.tl_dr[0]
    assert "rumored claim is stated" in script.writer_prompts[1]
    assert [c["role"] for c in llm.calls].count("Guardrail Agent") == 2


def test_a_retry_is_rechecked_only_by_the_guardrail_that_asked_for_it(hooks_on):
    """crewai semantics that decide guardrail order: the LLM rule's retry still meets the function checks
    (they come later), but a function check's retry is never shown to the LLM rule again."""
    speculative = brief(tl_dr=["Rumored: StockSathi will cut prices again [StockSathi-3-1]"])
    uncited = brief(tl_dr=["StockSathi will cut prices again"])
    script = Script(
        [speculative, uncited, brief()],
        guardrail_answers=[{"valid": False, "feedback": "Rumor stated as fact."}, {"valid": True}],
    )
    result, llm = run(script)
    assert len(script.writer_prompts) == 3  # LLM rule -> retry; citation check -> retry
    assert "tl_dr[0] cites no signal id" in script.writer_prompts[2]
    assert [c["role"] for c in llm.calls].count("Guardrail Agent") == 2  # never saw the third draft
    assert result.brief.tl_dr[0].endswith("[BahiBook-3-1]")


def test_guardrail_gives_up_after_max_retries(hooks_on):
    uncited = brief(tl_dr=["no citation"])
    with pytest.raises(Exception, match="validation after 2 retries"):
        run(Script([uncited, uncited, uncited]))


def test_crew_refuses_to_load_without_the_file_hook():
    with pytest.raises(RuntimeError, match="file_read_allowlist"):
        short_update.load()


def test_signals_input_is_compact():
    rows = json.loads(short_update.signals_input(REPORTS))
    assert {r["id"] for r in rows} == {"StockSathi-3-1", "StockSathi-3-2", "BahiBook-3-1"}
    assert set(rows[0]) == {"id", "company", "kind", "detail", "credibility", "novelty", "impact"}


def test_writer_gets_the_long_output_budget(hooks_on):
    from compintel.config import LONG_OUTPUT_MAX_TOKENS, get_settings

    analyst, writer = short_update.load().agents
    assert analyst.llm.max_tokens == get_settings().max_tokens  # type: ignore[union-attr]
    assert writer.llm.max_tokens == LONG_OUTPUT_MAX_TOKENS  # type: ignore[union-attr]
