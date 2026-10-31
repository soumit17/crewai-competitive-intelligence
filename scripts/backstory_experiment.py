"""Backstory-length experiment (spec §7, M3): does a 200-word backstory cost more, and does it judge better?

Runs ShortUpdateCrew's classify_signals task alone (same task, same week-2 signals) with the Market Analyst's
short backstory and a ~200-word one, REPEATS times each, and compares tokens, cost and threat levels
against data/ground_truth/week-2.json. Live, Haiku: about $0.003 in total.

    uv run compintel scout --week 2                   # once: the signals this experiment classifies
    uv run python scripts/backstory_experiment.py
"""

import json

from compintel.config import apply_env, get_settings

REPEATS = 2

LONG_BACKSTORY = (
    "You have spent fifteen years analysing competition in Indian retail technology, first as a "
    "consultant advising FMCG distributors on their route-to-market software, then as head of "
    "strategy at a point-of-sale startup in Bengaluru that was acquired by a larger payments company."
    " You know the kirana channel intimately: the economics of a single-counter store, the role of "
    "informal credit (udhaar) in customer loyalty, how GST compliance changed billing software "
    "adoption, and why price sensitivity is extreme below a thousand rupees a month. You have watched"
    " dozens of well-funded entrants announce expansion into new cities and fail to hire or retain a "
    "field sales force, and you have seen small accounting-first players quietly build features that "
    "later redefined the market. You now advise KiranaCloud's leadership team in Kolkata. They value "
    "your judgement because you weigh evidence carefully, separate verified facts from rumors and "
    "blog speculation, never inflate a threat to sound important, and always explain which specific "
    "signals drive your view. You write tersely, cite your sources, and prefer a calibrated 'medium' "
    "to a dramatic 'high' unless the evidence is new, credible and directly hits KiranaCloud's "
    "pricing, product roadmap or home market in West Bengal."
)


def main() -> None:
    apply_env()
    from crewai import Crew, Process
    from crewai.events.event_bus import crewai_event_bus

    from compintel import hooks, listeners
    from compintel.cost import get_ledger
    from compintel.crews import short_update
    from compintel.models import SignalReport, ThreatAssessments
    from compintel.world.story import GROUND_TRUTH

    listeners.install()
    hooks.install()
    scout_file = get_settings().p3_runs_dir / "scout" / "week-2.agents.json"
    reports = {c: SignalReport.model_validate(r) for c, r in json.loads(scout_file.read_text()).items()}
    expected = GROUND_TRUTH[2]["expected_threat"]
    ledger_path = get_ledger().path

    rows = []
    for label in ("short", "long"):
        for rep in range(1, REPEATS + 1):
            crew = short_update.load()
            analyst, classify = crew.agents[0], crew.tasks[0]
            if label == "long":
                analyst.backstory = LONG_BACKSTORY
            words = len(analyst.backstory.split())
            # The classify task on its own: a one-task crew with the same agent and task objects.
            mini = Crew(agents=[analyst], tasks=[classify], process=Process.sequential)
            before = len(ledger_path.read_text().splitlines()) if ledger_path.exists() else 0
            mini.kickoff(inputs={"week": 2, "signals": short_update.signals_input(reports)})
            crewai_event_bus.flush()
            new = [json.loads(line) for line in ledger_path.read_text().splitlines()[before:]]
            out = classify.output
            threats = out.pydantic if out and isinstance(out.pydantic, ThreatAssessments) else None
            levels = {t.company: t.threat for t in threats.threats} if threats else {}
            rows.append(
                {
                    "backstory": f"{label} ({words} words)",
                    "run": rep,
                    "calls": len(new),
                    "input_tokens": sum(
                        r["input_tokens"] + r["cache_read_tokens"] + r["cache_write_tokens"] for r in new
                    ),
                    "cache_read": sum(r["cache_read_tokens"] for r in new),
                    "output_tokens": sum(r["output_tokens"] for r in new),
                    "cost_usd": round(sum(r["cost_usd"] for r in new), 6),
                    "matches_ground_truth": f"{sum(levels.get(c) == lvl for c, lvl in expected.items())}/3",
                    "levels": levels,
                }
            )

    header = ["backstory", "run", "calls", "input_tokens", "cache_read", "output_tokens", "cost_usd"]
    header.append("matches_ground_truth")
    print(" | ".join(header))
    for r in rows:
        print(" | ".join(str(r[h]) for h in header), "|", r["levels"])
    print("ground truth:", expected)


if __name__ == "__main__":
    main()
