"""Process comparison (spec §8.3, M4): the same DeepDiveCrew agents and tasks run three ways on week 2.

    uv run python scripts/serve_fakeweb.py                      # other terminal (analysts' tools)
    uv run python scripts/process_comparison.py --variant sequential
    uv run python scripts/process_comparison.py --variant manager_llm
    uv run python scripts/process_comparison.py --variant manager_agent
    uv run python scripts/process_comparison.py --report         # writes docs/process-comparison.md

Each run appends its measurements to runs/process_comparison.json and keeps its brief under
runs/process_comparison/<variant>.md. Live, Haiku: roughly $0.03 per variant.
"""

import argparse
import json
import os
import shutil
import time
from collections import Counter

from compintel.config import PROJECT_ROOT, apply_env, get_settings

SPECIALISTS = {"Pricing Analyst", "Product Analyst", "Market Analyst", "Strategist", "Brief Writer"}
MANAGERS = {"Head of Strategy", "Crew Manager"}


def measure(variant: str) -> dict:
    apply_env()
    from crewai.events.event_bus import crewai_event_bus

    from compintel import hooks, listeners
    from compintel.cost import get_ledger
    from compintel.crews import deep_dive
    from compintel.listeners import DelegationLog, GuardrailLog
    from compintel.models import SignalReport
    from compintel.tools.render_brief import draft_path
    from compintel.world.ground_truth import check_brief

    listeners.install()
    hooks.install()
    os.chdir(PROJECT_ROOT)
    scout_file = get_settings().p3_runs_dir / "scout" / "week-2.agents.json"
    reports = {c: SignalReport.model_validate(r) for c, r in json.loads(scout_file.read_text()).items()}
    ledger = get_ledger()
    before_rows = len(ledger.path.read_text().splitlines()) if ledger.path.exists() else 0
    guard_log, deleg_log = GuardrailLog(), DelegationLog()

    started = time.perf_counter()
    error = None
    result = None
    try:
        result = deep_dive.run(2, reports, variant=variant)
    except Exception as e:  # a failed variant is a result too
        error = f"{type(e).__name__}: {str(e)[:300]}"
    seconds = round(time.perf_counter() - started, 1)
    crewai_event_bus.flush()

    rows = [json.loads(line) for line in ledger.path.read_text().splitlines()[before_rows:]]
    by_role = Counter(r["agent"] or "(no agent: knowledge query rewrite / planning internals)" for r in rows)
    planner_cost = sum(r["cost_usd"] for r in rows if r["agent"] == "Task Execution Planner")
    intended = getattr(result.crew, "intended_agents", {}) if result else {}
    out_dir = get_settings().p3_runs_dir / "process_comparison"
    out_dir.mkdir(parents=True, exist_ok=True)
    if result:
        shutil.copy(draft_path(2, ".md"), out_dir / f"{variant}.md")
    return {
        "variant": variant,
        "error": error,
        "seconds": seconds,
        "llm_calls": len(rows),
        "input_tokens": sum(
            r["input_tokens"] + r["cache_read_tokens"] + r["cache_write_tokens"] for r in rows
        ),
        "output_tokens": sum(r["output_tokens"] for r in rows),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
        "planner_cost_usd": round(planner_cost, 6),
        "calls_by_role": dict(by_role),
        "specialist_calls": sum(n for role, n in by_role.items() if role in SPECIALISTS),
        "manager_calls": sum(n for role, n in by_role.items() if role in MANAGERS),
        "delegations": deleg_log.entries,
        "delegated_to_intended": sum(
            1 for e in deleg_log.entries if e["coworker"] and e["coworker"] in intended.values()
        ),
        "guardrail_failures": [e["name"][:40] for e in guard_log.entries if not e["passed"]],
        "escalation_ran": bool(result and result.escalation_memo),
        "quality": check_brief(result.brief, reports, 2) if result else None,
    }


def quality(d: dict, key: str):
    return d["quality"][key] if d["quality"] else "-"


def delegations(d: dict) -> str:
    return f"{len(d['delegations'])} ({d['delegated_to_intended']})"


def kolkata_first(d: dict) -> str:
    return {True: "yes", False: "no"}.get(quality(d, "kolkata_first"), "-")


def report() -> None:
    data = json.loads((get_settings().p3_runs_dir / "process_comparison.json").read_text())
    latest = {d["variant"]: d for d in data}  # last run of each variant
    cols = [v for v in ("sequential", "manager_llm", "manager_agent") if v in latest]
    rows = [
        ("Wall time", lambda d: f"{d['seconds']:.0f} s"),
        ("LLM calls", lambda d: str(d["llm_calls"])),
        ("Input / output tokens", lambda d: f"{d['input_tokens']:,} / {d['output_tokens']:,}"),
        ("Cost (Haiku 5.5)", lambda d: f"${d['cost_usd']:.4f}"),
        ("of which crew planning", lambda d: f"${d['planner_cost_usd']:.4f}"),
        ("Manager calls", lambda d: str(d["manager_calls"])),
        ("Specialist calls", lambda d: str(d["specialist_calls"])),
        ("Delegations (to intended agent)", delegations),
        ("Guardrail failures", lambda d: str(len(d["guardrail_failures"]))),
        ("Escalation memo ran", lambda d: "yes" if d["escalation_ran"] else "no"),
        ("Material signals cited", lambda d: quality(d, "material_cited")),
        ("Threat levels vs ground truth", lambda d: f"{quality(d, 'threat_levels_match')}/3"),
        ("Kolkata threat leads the TL;DR", kolkata_first),
        ("Error", lambda d: d["error"] or "-"),
    ]  # fmt: skip
    lines = ["| | " + " | ".join(f"`{c}`" for c in cols) + " |", "|---|" + "---|" * len(cols)]
    lines += [f"| {name} | " + " | ".join(fn(latest[c]) for c in cols) + " |" for name, fn in rows]
    table = "\n".join(lines)
    path = PROJECT_ROOT / "docs" / "process-comparison.md"
    text = path.read_text() if path.exists() else ""
    begin, end = "<!-- table:begin -->", "<!-- table:end -->"
    if begin in text:
        text = text[: text.index(begin) + len(begin)] + "\n" + table + "\n" + text[text.index(end) :]
    else:
        text = f"# Process comparison (week 2, live)\n\n{begin}\n{table}\n{end}\n"
    path.write_text(text)
    print(table)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=["sequential", "manager_llm", "manager_agent"])
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    if args.report:
        report()
    elif args.variant:
        result = measure(args.variant)
        store = get_settings().p3_runs_dir / "process_comparison.json"
        data = json.loads(store.read_text()) if store.exists() else []
        store.write_text(json.dumps([*data, result], indent=2, ensure_ascii=False))
        print(
            json.dumps({k: v for k, v in result.items() if k != "delegations"}, indent=2, ensure_ascii=False)
        )
    else:
        parser.error("give --variant or --report")
