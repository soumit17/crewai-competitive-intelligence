"""Memory exercises (spec §11.1, M7), live on Haiku with the local embedder. About $0.01 in total.

    (a) consolidation: the same pricing fact remembered in week 1 and again in week 3 → one record or two?
    (b) recall depth: shallow vs deep on the same queries, cost and latency
    (c) private memories: a lesson private to the editor (source="editor"), invisible to everyone else

Uses its own store (runs/memory_exercises/), not the Flow's.

    uv run python scripts/memory_exercises.py
"""

import json
import shutil
import time

from compintel.config import apply_env, get_settings

QUERIES = [
    "What is StockSathi charging for its Basic plan?",
    "Which competitor is expanding into Kolkata, and how?",
    "What should the brief lead with?",
    # Over 200 characters, like the task prompt an agent recalls with: crewai's query_analysis_threshold
    # (200) sends only these through an LLM query analysis in deep recall.
    "Write this week's competitive brief for KiranaCloud's leadership: which competitor moves matter for "
    "our Kolkata kirana stores, what changed since last week on pricing and expansion, and what should we "
    "do about it in sales and product?",
]
FACTS = [
    ("/competitor/stocksathi", "StockSathi's Basic plan costs ₹499 a month with GST billing."),
    ("/competitor/stocksathi", "StockSathi cut its Pro plan from ₹999 to ₹699 a month in week 2."),
    ("/competitor/shelfmitra", "ShelfMitra raised a ₹200 crore Series B to expand into eastern India."),
    ("/competitor/shelfmitra", "ShelfMitra opened a Kolkata office for West Bengal retailers in week 3."),
    ("/competitor/bahibook", "BahiBook opened a GST e-invoicing product manager role in Pune."),
]


def main() -> None:
    apply_env()
    from crewai.events.event_bus import crewai_event_bus
    from crewai.memory.unified_memory import Memory

    from compintel import hooks, listeners
    from compintel.config import embedder_config, make_llm
    from compintel.cost import get_ledger

    listeners.install()
    hooks.install()
    path = get_settings().p3_runs_dir / "memory_exercises"
    shutil.rmtree(path, ignore_errors=True)
    mem = Memory(llm=make_llm(), embedder=embedder_config(), storage=str(path), recency_half_life_days=14)
    ledger = get_ledger()

    def measured(label, fn):
        crewai_event_bus.flush()
        rows, spent, started = _rows(ledger.path), ledger.spent_usd, time.perf_counter()
        result = fn()
        mem.drain_writes()
        crewai_event_bus.flush()
        print(f"  {label}: {time.perf_counter() - started:.2f}s, {_rows(ledger.path) - rows} LLM calls, "
              f"${ledger.spent_usd - spent:.6f}")  # fmt: skip
        return result

    fields = {"categories": ["pricing"], "importance": 0.6}
    print("(a) consolidation")
    fact = "StockSathi's Basic plan costs ₹499 a month."
    measured("week 1 remember", lambda: mem.remember(fact, scope="/competitor/stocksathi", **fields))
    measured(
        "week 3 remember (same fact)", lambda: mem.remember(fact, scope="/competitor/stocksathi", **fields)
    )
    stored = mem.recall("StockSathi Basic plan price", scope="/competitor/stocksathi", depth="shallow")
    print(f"  records now: {[m.record.content for m in stored]}")

    print("(b) recall depth")
    mem.reset_all()
    for scope, text in FACTS:
        mem.remember(text, scope=scope, **fields)
    mem.remember("Lead every brief with the single biggest threat to Kolkata.", scope="/hitl", **fields)
    for depth in ("shallow", "deep"):
        for q in QUERIES:
            hits = measured(
                f"{depth:7} {q[:45]!r}", lambda q=q, depth=depth: mem.recall(q, depth=depth, limit=3)
            )
            print(f"           top: {hits[0].record.content if hits else '-'}")

    print("(c) private memories")
    mem.remember("Editor only: never call a rumor 'confirmed'.", scope="/hitl", source="editor", private=True,
                 **fields)  # fmt: skip
    q = "rumor confirmed"
    for label, kwargs in (
        ("anyone", {}),
        ("source=editor", {"source": "editor"}),
        ("source=writer", {"source": "writer"}),
    ):
        hits = [m.record.content for m in mem.recall(q, scope="/hitl", depth="shallow", **kwargs)]
        print(f"  {label:14}: sees the private lesson: {'Editor only' in ' '.join(hits)}")


def _rows(path) -> int:
    return len(path.read_text().splitlines()) if path.exists() else 0


if __name__ == "__main__":
    main()
    print(json.dumps({"done": True}))
