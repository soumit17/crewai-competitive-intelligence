"""Spec Rule 3: the project runs with OPENAI_API_KEY unset and never builds an OpenAI client.

Skeleton in M0 (the hello agent). Each later milestone adds its component here: crews (M3/M4), the flow (M5),
human feedback `emit`/`learn` (M6), memory + knowledge embedders (M4/M7).
"""

import os

import openai
import pytest
from crewai.llms.providers.anthropic.completion import AnthropicCompletion
from crewai.utilities.llm_utils import create_llm

from compintel import hello
from tests.fakes import Rule, ScriptedLLM
from tests.test_flow import stubs  # noqa: F401 - fixture: scouting and crews stubbed

TAKE = {"company": "StockSathi", "kind": "pricing_change", "impact_on_us": "high", "why": "Undercuts us."}


@pytest.fixture
def openai_forbidden(monkeypatch):
    def boom(*_, **__):
        raise AssertionError("an OpenAI client was constructed")

    monkeypatch.setattr(openai.OpenAI, "__init__", boom)
    monkeypatch.setattr(openai.AsyncOpenAI, "__init__", boom)


def test_openai_key_is_absent():
    assert "OPENAI_API_KEY" not in os.environ


def test_unconfigured_component_falls_back_to_anthropic_not_gpt():
    # What crewai builds for anything left without an llm (planner, manager, ...): the MODEL env safety net.
    llm = create_llm(None)
    assert isinstance(llm, AnthropicCompletion)
    assert llm.model == "claude-haiku-5-5"


def test_hello_agent_runs_without_openai(openai_forbidden):
    out = hello.run(llm=ScriptedLLM(Rule(TAKE)))
    assert out.pydantic is not None


def test_short_update_crew_runs_without_openai(openai_forbidden):
    from compintel import hooks
    from tests.test_short_update_crew import Script, brief, run

    hooks.install()
    result, _ = run(Script([brief()]))
    assert result.brief.route == "routine"


def test_deep_dive_runs_without_openai(openai_forbidden, world):
    from compintel import hooks
    from compintel.crews import deep_dive
    from tests.deep_dive_script import WEEK2_REPORTS, DeepDiveScript

    hooks.install()
    result = deep_dive.run(
        2, WEEK2_REPORTS, variant="manager_agent", fake=ScriptedLLM(Rule(DeepDiveScript()))
    )
    assert result.brief.route == "material"


def test_crewai_storage_is_outside_the_developers_real_data_dir():
    from pathlib import Path

    from crewai.rag.chromadb.constants import DEFAULT_STORAGE_PATH

    assert "compintel-tests" in DEFAULT_STORAGE_PATH
    assert not DEFAULT_STORAGE_PATH.startswith(str(Path.home() / ".local" / "share"))


def test_review_model_and_flow_memory_are_anthropic_and_local():
    from compintel.flow import REVIEW_LLM, CompetitiveIntelFlow

    assert isinstance(
        REVIEW_LLM, AnthropicCompletion
    )  # @human_feedback(llm=...): collapse, distill, pre-review
    memory = CompetitiveIntelFlow().memory  # not the Memory() a Flow builds itself (gpt-5.4-mini + OpenAI)
    assert memory is not None
    assert isinstance(memory.llm, AnthropicCompletion)  # type: ignore[union-attr]
    assert memory.embedder["provider"] != "openai"  # type: ignore[union-attr,index]


def test_a_reviewed_week_with_lessons_runs_without_openai(openai_forbidden, world, stubs, monkeypatch):  # noqa: F811
    from compintel.flow import run_week
    from tests import hitl

    hitl.install(monkeypatch, hitl.ScriptedReviewer(("Lead with Kolkata.", "revise")))
    from compintel.crews import revision

    monkeypatch.setattr(revision, "run", lambda brief, *a, **k: brief)
    assert run_week(1).state.published_path == "briefs/week-01.md"
