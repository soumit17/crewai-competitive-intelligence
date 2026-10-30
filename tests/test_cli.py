from typer.testing import CliRunner

from compintel import config
from compintel.cli import app
from tests.fakes import Rule, ScriptedLLM

TAKE = {"company": "StockSathi", "kind": "pricing_change", "impact_on_us": "high", "why": "Undercuts us."}
runner = CliRunner()


def test_hello_end_to_end_with_scripted_llm(monkeypatch):
    # Same bootstrap as a live run (listener + budget hook + ledger); only the model is fake.
    monkeypatch.setattr(config, "make_llm", lambda final=False: ScriptedLLM(Rule(TAKE)))
    result = runner.invoke(app, ["hello", "ShelfMitra opens a Kolkata office."])
    assert result.exit_code == 0, result.output
    assert '"impact_on_us": "high"' in result.output
    assert "this run: $0.000150" in result.output  # 1000 in + 100 out at Haiku prices

    costs = runner.invoke(app, ["costs"])
    assert costs.exit_code == 0, costs.output
    assert "P3 total $0.000150 of $12.00 cap" in costs.output


def test_costs_with_empty_ledger():
    result = runner.invoke(app, ["costs"])
    assert result.exit_code == 0
    assert "no spend recorded yet" in result.output
