"""M7 Ask the Analyst: routing, the context each route answers from, and a session that continues in a new
Flow instance (as in a new process). $0: the scripted model routes by keyword and answers with its context."""

import json
from pathlib import Path

import pytest

from compintel import chat
from compintel.chat import AskAnalyst
from compintel.memory import brief_scope
from tests.fakes import Rule, ScriptedLLM
from tests.test_flow import brief_for

ROUTES = {"when": "ask_fact", "compare": "compare", "changed": "what_changed", "bye": "end"}


def reply(messages, response_model):
    if response_model is not None and "intent" in response_model.model_fields:  # crewai's router call
        # The router's user message is a JSON context: route on the current message, not the route catalog.
        text = json.loads(messages[-1]["content"])["current_user_message"].lower()
        return {"intent": next((r for k, r in ROUTES.items() if k in text), "converse")}
    return "ANSWER\n" + str(messages[0]["content"])  # echo the context the handler built


@pytest.fixture
def llm(monkeypatch):
    fake = ScriptedLLM(Rule(reply))
    monkeypatch.setattr(AskAnalyst, "conversational_config", chat.config(fake))
    return fake


def test_a_fact_question_is_answered_from_memory_with_its_sources(llm):
    fields = {"categories": ["brief"], "importance": 0.7, "source": "brief"}
    brief_scope(2).remember(
        "ShelfMitra is hiring 15 sales staff in Kolkata.", metadata={"file": "briefs/week-02.md"}, **fields
    )
    brief_scope(3).remember(
        "ShelfMitra opened a Kolkata office for West Bengal retailers.",
        metadata={"file": "briefs/week-03.md"},
        **fields,
    )
    flow = AskAnalyst()
    answer = flow.handle_turn("What did ShelfMitra do in Kolkata and when?")
    assert flow.state.last_intent == "ask_fact"
    assert "hiring 15 sales staff in Kolkata. [briefs/week-02.md]" in answer
    assert "opened a Kolkata office for West Bengal retailers. [briefs/week-03.md]" in answer


def test_compare_reads_the_threat_tables_of_every_published_week(llm):
    Path("briefs").mkdir()
    for week, route in ((1, "routine"), (2, "material")):
        Path(f"briefs/week-{week:02d}.json").write_text(brief_for(week, route).model_dump_json())
    Path("briefs/week-02.feedback.json").write_text(json.dumps({"reviews": []}))  # not a brief
    answer = AskAnalyst().handle_turn("Compare StockSathi and ShelfMitra over the weeks")
    assert "week 1, StockSathi: low" in answer and "week 2, ShelfMitra: high" in answer
    assert "[briefs/week-02.md]" in answer


def test_what_changed_reads_the_two_weeks_asked_about(llm):
    Path("briefs").mkdir()
    for week in (1, 2, 3):
        Path(f"briefs/week-{week:02d}.md").write_text(f"# Week {week:02d}: headline {week}\n")
    answer = AskAnalyst().handle_turn("What changed between week 1 and week 3?")
    assert "=== briefs/week-01.md ===" in answer and "=== briefs/week-03.md ===" in answer
    assert "=== briefs/week-02.md ===" not in answer


def test_a_session_continues_in_a_new_instance(llm):
    first = AskAnalyst()
    first.handle_turn("Hello there")
    session = first.state.id
    second = AskAnalyst()  # a new process would do the same: @persist restores the session by id
    second.handle_turn("Thanks, bye", session_id=session)
    assert [m.role for m in second.state.messages] == ["user", "assistant", "user", "assistant"]
    assert second.state.ended  # the built-in "end" route
