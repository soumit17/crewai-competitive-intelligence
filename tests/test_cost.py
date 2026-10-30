import json

import pytest
from crewai.events.event_bus import crewai_event_bus
from crewai.hooks import HookAborted

from compintel import hello, hooks, listeners
from compintel.cost import CostLedger, get_ledger, price, split_usage
from tests.fakes import Rule, ScriptedLLM

TAKE = {"company": "StockSathi", "kind": "pricing_change", "impact_on_us": "high", "why": "Undercuts us."}


def test_cache_tokens_are_priced_once_at_their_own_rate():
    # crewai reports input_tokens *including* cache reads/writes.
    usage = {
        "input_tokens": 1300,
        "output_tokens": 100,
        "cached_prompt_tokens": 200,
        "cache_creation_tokens": 100,
    }
    assert split_usage(usage) == {"input": 1000, "cache_write": 100, "cache_read": 200, "output": 100}
    expected = (1000 * 0.10 + 100 * 0.125 + 200 * 0.01 + 100 * 0.50) / 1e6
    assert price("claude-haiku-5-5", usage) == pytest.approx(expected)
    assert price("anthropic/claude-haiku-5-5", usage) == pytest.approx(expected)


def test_unknown_model_is_an_error_not_free():
    with pytest.raises(KeyError):
        price("gpt-4.1-mini", {"input_tokens": 1})


def test_ledger_reloads_total_and_skips_replayed_responses(tmp_path):
    path = tmp_path / "costs.jsonl"
    ledger = CostLedger(path, cap_usd=1.0)
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    assert ledger.record("claude-haiku-5-5", usage, response_id="r1") == pytest.approx(0.10)
    assert ledger.record("claude-haiku-5-5", usage, response_id="r1") == 0.0
    reloaded = CostLedger(path, cap_usd=1.0)
    assert reloaded.spent_usd == pytest.approx(0.10)
    assert len(path.read_text().splitlines()) == 1


def test_listener_writes_one_row_per_llm_call_with_agent_role():
    listeners.install()
    hello.run(llm=ScriptedLLM(Rule(TAKE)))
    crewai_event_bus.flush()  # handlers run on the bus's thread pool
    rows = [json.loads(line) for line in get_ledger().path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["project"] == "p3"
    assert rows[0]["agent"] == "Competitive Signal Scout"
    assert rows[0]["cost_usd"] == pytest.approx((1000 * 0.10 + 100 * 0.50) / 1e6)


def test_budget_gate_aborts_before_the_model_is_called():
    get_ledger().record("claude-haiku-5-5", {"input_tokens": 200_000_000}, response_id="big")  # $20 > $12 cap
    hooks.install()
    llm = ScriptedLLM(Rule(TAKE))
    with pytest.raises(HookAborted, match="spend cap"):
        hello.run(llm=llm)
    assert llm.calls == []


def test_budget_gate_lets_calls_through_under_the_cap():
    hooks.install()
    out = hello.run(llm=ScriptedLLM(Rule(TAKE)))
    assert out.pydantic is not None
