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


def test_scout_command_checks_reports_against_ground_truth(world, monkeypatch):
    from compintel import scouting
    from tests.test_scout import ScriptedScout

    week2 = {  # what a good week-2 scout reports, per company
        "StockSathi": [
            ("pricing_change", "Pro cut 30% to ₹699/month"),
            ("feature_launch", "UPI Udhaar credit"),
        ],
        "ShelfMitra": [
            ("funding", "Series B of ₹200 crore"),
            ("hiring_expansion", "15 sales roles in Kolkata"),
        ],
        "BahiBook": [],
    }

    class GoodScout(ScriptedScout):
        def __call__(self, messages, response_model):
            out = super().__call__(messages, response_model)
            if isinstance(out, dict):  # the SignalReport conversion: fill in the signals
                c = out["company"]
                out["signals"] = [
                    {"id": f"{c}-2-{i}", "company": c, "kind": k, "detail": d, "source_ref": "x",
                     "source_credibility": 0.9, "novelty": "new", "impact_on_us": "high"}
                    for i, (k, d) in enumerate(week2[c], 1)
                ]  # fmt: skip
            return out

    monkeypatch.setattr(scouting, "make_llm", lambda **_: ScriptedLLM(Rule(GoodScout(world, 2))))
    result = runner.invoke(app, ["scout", "--week", "2"])
    assert result.exit_code == 0, result.output
    assert result.output.count("✓ material_signals") == 4
    assert "✗" not in result.output
    assert "via agents:" in result.output


def test_scout_command_explains_a_missing_fake_web(monkeypatch):
    monkeypatch.setenv("FAKEWEB_URL", "http://127.0.0.1:9")  # nothing listens on port 9
    from compintel.config import get_settings

    get_settings.cache_clear()
    result = runner.invoke(app, ["scout", "--week", "1"])
    assert result.exit_code != 0
    assert "serve_fakeweb.py" in result.output


def test_brief_command_runs_the_crew_on_saved_scout_reports(tmp_path, monkeypatch):
    import json as _json

    from compintel import config
    from compintel.crews import short_update
    from tests.test_short_update_crew import REPORTS, Script, brief

    scout_file = tmp_path / "runs" / "scout" / "week-3.agents.json"
    scout_file.parent.mkdir(parents=True)
    scout_file.write_text(_json.dumps({c: r.model_dump() for c, r in REPORTS.items()}))
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)  # the CLI chdirs here before running the crew
    real_run = short_update.run
    script = Script([brief(tl_dr=["uncited"]), brief()])  # one function-guardrail retry
    monkeypatch.setattr(
        short_update, "run", lambda w, r, final=False: real_run(w, r, final, fake=ScriptedLLM(Rule(script)))
    )

    result = runner.invoke(app, ["brief", "--week", "3"])
    assert result.exit_code == 0, result.output
    assert "# Week 03: BahiBook eyes GST e-invoicing" in result.output
    assert "every_claim_cites_signal' attempt 1: FAIL → retry" in result.output
    assert "every_claim_cites_signal' attempt 2: pass" in result.output
    assert (tmp_path / "briefs" / "drafts" / "week-03.md").exists()


def test_brief_command_needs_scout_reports_first():
    result = runner.invoke(app, ["brief", "--week", "1"])
    assert result.exit_code != 0 and "compintel scout --week 1" in result.output


def test_run_command_runs_a_week_through_the_flow(world, monkeypatch, tmp_path):
    from compintel import config
    from tests.test_flow import WEEKS, brief_for

    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    from types import SimpleNamespace

    from compintel import scouting
    from compintel.crews import short_update

    async def scout_all(week, llm=None, verbose=False):
        return WEEKS[week]

    monkeypatch.setattr(scouting, "scout_all", scout_all)
    monkeypatch.setattr(
        short_update, "run", lambda week, reports, **k: SimpleNamespace(brief=brief_for(week, "routine"))
    )
    result = runner.invoke(app, ["run", "--week", "1"])
    assert result.exit_code == 0, result.output
    assert "route routine" in result.output and "published briefs/week-01.md" in result.output
    assert "flow.usage_metrics:" in result.output
    assert (tmp_path / "briefs" / "week-01.md").exists()
