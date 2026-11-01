from crewai.llms.providers.anthropic.completion import AnthropicCompletion

from compintel import hello
from compintel.config import AGENT_LIMITS, embedder_config, make_llm
from tests.fakes import Rule, ScriptedLLM

TAKE = {"company": "StockSathi", "kind": "pricing_change", "impact_on_us": "high", "why": "Undercuts us."}


def test_agent_kickoff_returns_the_typed_model_in_one_call():
    llm = ScriptedLLM(Rule(TAKE, role="Competitive Signal Scout"))
    out = hello.run("BahiBook fixes a GST rounding bug.", llm=llm)

    assert isinstance(out.pydantic, hello.HeadlineTake)
    assert out.pydantic.impact_on_us == "high"
    # response_format reaches the LLM as response_model: crewai asks for the schema, no ReAct parsing.
    assert len(llm.calls) == 1
    assert llm.calls[0]["response_model"] is hello.HeadlineTake
    assert "BahiBook fixes a GST rounding bug." in str(llm.calls[0]["messages"])


def test_usage_metrics_match_what_the_llm_reported():
    out = hello.run(llm=ScriptedLLM(Rule(TAKE)))
    assert out.usage_metrics["prompt_tokens"] == 1000
    assert out.usage_metrics["completion_tokens"] == 100
    assert out.usage_metrics["successful_requests"] == 1


def test_agent_has_explicit_limits():
    agent = hello.make_agent(ScriptedLLM())
    for name, value in AGENT_LIMITS.items():
        assert getattr(agent, name) == value


def test_make_llm_uses_the_native_anthropic_provider():
    dev, final = make_llm(), make_llm(final=True)
    assert isinstance(dev, AnthropicCompletion) and isinstance(final, AnthropicCompletion)
    assert dev.model == "claude-haiku-5-5"
    assert final.model == "claude-sonnet-5-5"
    assert dev.max_tokens == 1500
    assert dev.temperature is None  # Claude 5.5 models reject non-default sampling values


def test_embedder_is_local(monkeypatch):
    from compintel import config

    monkeypatch.setattr(config, "_embedder_override", None)  # the real config, not conftest's test fake
    assert embedder_config() == {"provider": "onnx", "config": {}}
