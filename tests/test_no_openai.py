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
