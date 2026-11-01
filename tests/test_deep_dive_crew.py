"""M4 DeepDiveCrew: @CrewBase + YAML, three process variants, ConditionalTask, knowledge, planning. All $0."""

import json
from pathlib import Path

import pytest
from crewai import Process
from crewai.knowledge.knowledge import Knowledge
from crewai.tasks.conditional_task import ConditionalTask
from crewai.tasks.llm_guardrail import LLMGuardrail

from compintel import guardrails, hooks, knowledge
from compintel.config import AGENT_LIMITS, LONG_OUTPUT_MAX_TOKENS, get_settings
from compintel.crews import deep_dive
from compintel.crews.deep_dive import DeepDiveCrew
from compintel.listeners import DelegationLog
from compintel.models import Brief, Synthesis, ThreatAssessments
from tests.deep_dive_script import WEEK2_REPORTS, DeepDiveScript, brief
from tests.fakes import FAKE_EMBEDDER_SPEC, Rule, ScriptedLLM

SPECIALISTS = ["Pricing Analyst", "Product Analyst", "Market Analyst", "Strategist", "Brief Writer"]
TASKS = [
    "pricing_analysis",
    "product_overlap",
    "market_threats",
    "synthesis",
    "escalation_memo",
    "write_brief",
]


@pytest.fixture
def on(world):
    """Hooks installed, tools and knowledge pointed at the generated test world."""
    hooks.install()
    return world


def run(variant="sequential", **script_kwargs):
    script = DeepDiveScript(**script_kwargs)
    llm = ScriptedLLM(Rule(script))
    return deep_dive.run(2, WEEK2_REPORTS, variant=variant, fake=llm), script, llm


# ---------------------------------------------------------------- the definition
def test_sequential_crew_from_yaml(on):
    crew = DeepDiveCrew(variant="sequential").crew()
    assert crew.process == Process.sequential and crew.manager_agent is None
    assert [a.role for a in crew.agents] == SPECIALISTS
    assert [t.name for t in crew.tasks] == TASKS
    by = {t.name: t for t in crew.tasks}
    assert [t.async_execution for t in crew.tasks] == [True, True, True, False, False, False]
    assert by["synthesis"].context == [by["pricing_analysis"], by["product_overlap"], by["market_threats"]]
    assert by["market_threats"].output_pydantic is ThreatAssessments
    assert by["synthesis"].output_pydantic is Synthesis and by["write_brief"].output_pydantic is Brief
    memo = by["escalation_memo"]
    assert (
        isinstance(memo, ConditionalTask) and memo.condition is guardrails.any_high_threat and memo.markdown
    )
    write_guards = by["write_brief"].guardrails
    assert (
        isinstance(write_guards[0], LLMGuardrail)
        and write_guards[0].description == guardrails.NO_RUMOR_AS_FACT
    )  # type: ignore[index]
    assert write_guards[1:] == [  # type: ignore[index]
        guardrails.every_claim_cites_signal,
        guardrails.low_credibility_only_discounted,
        guardrails.brief_length_ok,
    ]


def test_models_planning_knowledge_and_limits_come_from_config(on):
    crew = DeepDiveCrew(variant="sequential").crew()
    agents = {a.role: a for a in crew.agents}
    assert crew.planning and crew.planning_llm.model == "claude-haiku-5-5"  # type: ignore[union-attr]
    for a in crew.agents:
        assert a.llm.model == "claude-haiku-5-5" and a.llm.max_tokens == LONG_OUTPUT_MAX_TOKENS  # type: ignore[union-attr]
        assert a.embedder == FAKE_EMBEDDER_SPEC  # config.embedder_config(), here the test fake
        for name, value in AGENT_LIMITS.items():
            assert getattr(a, name) == value
    strategist, market = agents["Strategist"].planning_config, agents["Market Analyst"].planning_config
    assert (strategist.reasoning_effort, strategist.max_attempts, strategist.max_steps) == ("medium", 2, 6)  # type: ignore[union-attr]
    assert (market.reasoning_effort, market.max_attempts) == ("low", 1)  # type: ignore[union-attr]
    for plan in (
        strategist,
        market,
    ):  # the loop bounds the spec leaves at their defaults (3 replans, 15 iterations)
        assert (plan.max_replans, plan.max_step_iterations) == (1, 3)  # type: ignore[union-attr]
    assert strategist.llm.model == "claude-haiku-5-5"  # type: ignore[union-attr]
    assert agents["Pricing Analyst"].knowledge_sources[0].file_paths == [
        get_settings().p3_knowledge_dir / knowledge.PRICING
    ]  # type: ignore[index]
    assert len(crew.knowledge_sources) == 2  # type: ignore[arg-type]
    final = DeepDiveCrew(variant="sequential", final=True).crew()
    assert {a.role: a.llm.model for a in final.agents}["Brief Writer"] == "claude-sonnet-5-5"  # type: ignore[union-attr]


@pytest.mark.parametrize("variant", ["manager_llm", "manager_agent"])
def test_hierarchical_variants_let_the_manager_choose(on, variant):
    base = DeepDiveCrew(variant=variant)
    crew = base.crew()
    assert crew.process == Process.hierarchical
    assert all(t.agent is None for t in crew.tasks)  # the manager decides who does what
    assert all(not t.tools for t in crew.tasks)  # ...and only through delegation, not the specialists' tools
    assert not any(t.async_execution for t in crew.tasks)  # one manager executor: no concurrency
    assert base.intended_agents["write_brief"] == "Brief Writer"
    if variant == "manager_agent":
        assert crew.manager_agent.role == "Head of Strategy" and crew.manager_agent not in crew.agents  # type: ignore[union-attr]
        assert not crew.manager_agent.tools  # type: ignore[union-attr]
    else:
        assert crew.manager_agent is None and crew.manager_llm.model == "claude-haiku-5-5"  # type: ignore[union-attr]


def test_unknown_variant_is_rejected():
    with pytest.raises(ValueError, match="variant"):
        DeepDiveCrew(variant="democracy")


# ---------------------------------------------------------------- scripted runs
def test_sequential_run_writes_the_brief_and_runs_the_escalation(on):
    result, script, llm = run("sequential")
    assert result.brief.route == "material"
    assert result.synthesis is not None and {t.threat for t in result.synthesis.threats} >= {"high"}
    assert result.escalation_memo is not None and result.escalation_memo.startswith("# Escalation")
    assert Brief.model_validate_json(Path("briefs/drafts/week-02.json").read_text()) == result.brief
    assert Path("briefs/drafts/week-02.md").read_text().startswith("# Week 02:")
    usage = [
        json.loads(line)
        for line in (get_settings().p3_runs_dir / "crew_usage.jsonl").read_text().splitlines()
    ]
    assert usage[-1]["variant"] == "sequential" and usage[-1]["token_usage"]["successful_requests"] > 0
    assert llm.calls[0]["role"] == "Task Execution Planner"  # crew planning runs before any task
    # Knowledge retrieval: crewai rewrites each task prompt into a search query with one extra LLM call.
    assert script.query_rewrites >= len(TASKS)


def test_escalation_memo_is_skipped_without_a_high_threat(on):
    result, _, _ = run("sequential", high=False, briefs=[brief(high=False)])
    assert result.escalation_memo is None
    assert result.brief.route == "material"


@pytest.mark.parametrize("variant", ["manager_llm", "manager_agent"])
def test_manager_delegates_every_task_through_crewais_tool(on, variant):
    log = DelegationLog()
    result, script, llm = run(variant)
    assert result.brief.route == "material"
    expected = [
        "Pricing Analyst",
        "Product Analyst",
        "Market Analyst",
        "Strategist",
        "Strategist",
        "Brief Writer",
    ]
    assert [e["coworker"] for e in log.entries] == expected
    manager = "Head of Strategy" if variant == "manager_agent" else "Crew Manager"
    assert {e["by"] for e in log.entries} == {manager} and {e["kind"] for e in log.entries} == {"delegate"}
    # The wiring the live comparison relies on: every manager call is offered crewai's delegation tools, so a
    # manager that never delegates (as Haiku didn't, live) chose not to; it wasn't unable to.
    manager_tools = [c["tools"] for c in llm.calls if c["role"] == manager]
    assert manager_tools and all(
        set(t) == {"delegate_work_to_coworker", "ask_question_to_coworker"} for t in manager_tools
    )


def test_brief_guardrail_retry_in_the_deep_dive(on):
    uncited = brief(tl_dr=["ShelfMitra is hiring in Kolkata"])
    result, script, _ = run("sequential", briefs=[uncited, brief()])
    assert len(script.writer_prompts) == 2
    assert "tl_dr[0] cites no signal id" in script.writer_prompts[1]
    assert result.brief.tl_dr[0].endswith("[ShelfMitra-2-2]")


def test_deep_dive_refuses_to_run_without_the_file_hook(world):
    with pytest.raises(RuntimeError, match="file_read_allowlist"):
        deep_dive.run(2, WEEK2_REPORTS, fake=ScriptedLLM())


# ---------------------------------------------------------------- knowledge, offline
def test_knowledge_sources_retrieve_from_markdown_and_pdf(on):
    k = Knowledge(collection_name="test_kb", sources=knowledge.crew_sources(), embedder=FAKE_EMBEDDER_SPEC)
    k.add_sources()
    hits = " ".join(
        r["content"] for r in k.query(["Dukaan Rs 649 board decision"], results_limit=5, score_threshold=0.0)
    )
    assert "649" in hits
    roadmap = " ".join(
        r["content"] for r in k.query(["GST e-invoicing Q1 2027"], results_limit=5, score_threshold=0.0)
    )
    assert "e-invoicing" in roadmap
