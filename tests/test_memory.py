"""M7 unified memory: scopes, slices, private records, consolidation, and who reads and writes what.
$0: hash embedder, LanceDB in tmp, the scripted memory model from conftest (tests/fakes.memory_reply)."""

import asyncio
import json
import re

from compintel import memory
from compintel.memory import brief_scope, competitor_scope, hitl_scope, shared, writer_slice
from tests.fakes import Rule, ScriptedLLM, memory_reply
from tests.test_flow import stubs  # noqa: F401 - fixture
from tests.test_scout import scout_llm  # noqa: F401 - fixture

FIELDS = {"categories": ["fact"], "importance": 0.6}  # given → no LLM field analysis on save


def contents(matches):
    return [m.record.content for m in matches]


def test_a_competitor_scope_sees_only_its_competitor():
    competitor_scope("StockSathi").remember("StockSathi cut its Pro plan to ₹699 a month.", **FIELDS)
    competitor_scope("ShelfMitra").remember("ShelfMitra raised a ₹200 crore Series B.", **FIELDS)
    seen = competitor_scope("StockSathi").recall("plan price funding Series B", limit=10)
    assert contents(seen) == ["StockSathi cut its Pro plan to ₹699 a month."]
    assert seen[0].record.scope == "/competitor/stocksathi"  # the scope's root, not an LLM-inferred path


def test_a_scout_reads_its_competitors_memory_and_writes_nothing(scout_llm):  # noqa: F811
    from compintel import scouting

    script, llm = scout_llm
    competitor_scope("ShelfMitra").remember("Week 1: ShelfMitra posted Mumbai hiring.", **FIELDS)
    competitor_scope("StockSathi").remember("Week 1: StockSathi's Basic plan is ₹499.", **FIELDS)
    asyncio.run(scouting.scout_company("ShelfMitra", 2, llm=llm))
    first = json.dumps(llm.calls[0]["messages"], default=str)  # crewai recalled before the kickoff
    assert "ShelfMitra posted Mumbai hiring" in first and "StockSathi's Basic plan" not in first
    assert "save_to_memory" not in llm.calls[0]["tools"]
    everything = shared().recall("ShelfMitra StockSathi week", depth="shallow", limit=50)
    assert len(everything) == 2  # the scout's kickoff saved nothing (the Flow records signals at publish)


def test_the_writer_slice_is_read_only_and_spans_three_scopes():
    view = writer_slice()
    assert view.read_only and view.remember("A draft is not a fact.", scope="/brief") is None  # silent no-op
    brief_scope(1).remember("Week 1: StockSathi's Basic plan is ₹499.", **FIELDS)
    competitor_scope("BahiBook").remember("BahiBook hired a GST e-invoicing PM.", **FIELDS)
    hitl_scope().remember("Lead with the biggest Kolkata threat.", source="hitl", **FIELDS)
    found = contents(view.recall("StockSathi BahiBook Kolkata", limit=10))
    assert "A draft is not a fact." not in found
    assert len(found) == 3


def test_a_private_lesson_is_visible_only_to_its_source():
    hitl_scope().remember("Editor only: never say 'synergy'.", source="editor", private=True, **FIELDS)
    hitl_scope().remember("Lead with the biggest Kolkata threat.", source="hitl", **FIELDS)
    assert contents(hitl_scope().recall("synergy Kolkata")) == ["Lead with the biggest Kolkata threat."]
    assert "Editor only: never say 'synergy'." in contents(hitl_scope().recall("synergy", source="editor"))


def test_consolidation_updates_a_repeated_fact_instead_of_duplicating_it(monkeypatch):
    def consolidate(messages, response_model):
        if getattr(response_model, "__name__", "") == "ConsolidationPlan":
            record_id = re.search(r"id=(\S+)", str(messages[-1]["content"])).group(1)  # type: ignore[union-attr]
            return {
                "actions": [
                    {"action": "update", "record_id": record_id, "new_content": "Basic: ₹499 (weeks 1, 3)."}
                ],
                "insert_new": False,
            }
        return memory_reply(messages, response_model)

    llm = ScriptedLLM(Rule(consolidate))
    monkeypatch.setattr(memory, "make_llm", lambda **_: llm)
    memory._shared.cache_clear()
    scope = competitor_scope("StockSathi")
    fact = "StockSathi Basic plan costs ₹499 per month."  # seen again on week 3's pricing page
    scope.remember(fact, metadata={"week": 1}, **FIELDS)
    scope.remember(fact, metadata={"week": 3}, **FIELDS)
    assert contents(scope.recall("Basic plan price", limit=10)) == ["Basic: ₹499 (weeks 1, 3)."]
    assert sum(getattr(c["response_model"], "__name__", "") == "ConsolidationPlan" for c in llm.calls) == 1


def test_the_writer_recalls_earlier_weeks_before_writing():
    """The M7 acceptance's mechanism: week N's Brief Writer sees facts published in earlier weeks."""
    from compintel import hooks
    from compintel.crews import short_update
    from tests.test_short_update_crew import REPORTS, Script, brief

    hooks.install()
    fact = "ShelfMitra posted 15 Kolkata sales roles in week 2."
    brief_scope(2).remember(fact, metadata={"week": 2, "file": "briefs/week-02.md"}, **FIELDS)
    llm = ScriptedLLM(Rule(Script([brief()])))
    short_update.run(3, REPORTS, fake=llm)
    writer = [json.dumps(c["messages"], default=str) for c in llm.calls if c["role"] == "Brief Writer"]
    analyst = [json.dumps(c["messages"], default=str) for c in llm.calls if c["role"] == "Market Analyst"]
    assert fact in writer[0]  # recalled from the slice into the write task's prompt
    assert all(fact not in p for p in analyst)  # the analyst has no memory


def test_publish_remembers_the_briefs_facts_with_their_file(world, stubs):  # noqa: F811
    from compintel.flow import run_week

    run_week(1)
    facts = brief_scope(1).recall("Week 1 StockSathi", limit=20)
    assert facts
    assert {(m.record.metadata["file"], m.record.source) for m in facts} == {("briefs/week-01.md", "brief")}
    assert hitl_scope().recall("Week 1 StockSathi") == []  # brief facts never land where lessons are read
    signals = competitor_scope("StockSathi").recall("StockSathi week 1", limit=20)
    assert {m.record.metadata["signal_id"] for m in signals} == {"StockSathi-1-1"}  # tests.test_flow.WEEKS[1]
    assert signals[0].record.content.startswith("Week 1: ")
