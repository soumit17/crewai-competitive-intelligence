"""Ask the Analyst (spec §14): a conversational Flow over what CompIntel has learned.

crewai's conversational Flow does the chat plumbing: `handle_turn(message, session_id=...)` appends the user
message, an LLM router picks a route from the `@listen` handlers (their docstrings are the route catalog), the
handler's return value becomes the assistant's reply, and `@persist` keeps the session so a later process can
continue it. Routes:

    ask_fact      a factual question → recall from memory (read-only slice over /competitor and /brief)
    compare       competitors over weeks → the published briefs' threat tables
    what_changed  difference between two weeks → the two published briefs
    converse      anything else (crewai's built-in route: plain chat over the history)
    end           goodbye (built-in)

Retrieval is done in code (a shallow recall, reading briefs/), then one model call answers from it: two calls
per turn with the router. An agent with recall and file tools would decide the same reads in a ReAct loop.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from crewai.flow.conversational import ConversationConfig, ConversationState, RouterConfig
from crewai.flow.flow import Flow, listen
from crewai.flow.persistence import persist

from compintel.config import make_llm
from compintel.memory import analyst_slice

BRIEFS_DIR = Path("briefs")  # cwd-relative, like the Flow's

SYSTEM = (
    "You are KiranaCloud's competitive-intelligence analyst (KiranaCloud: Kolkata kirana-store SaaS, Dukaan "
    "plan ₹799/month). Competitors: StockSathi, ShelfMitra, BahiBook. Answer only from the context given; if "
    "it doesn't say, say you don't know. Be brief. Cite where each fact comes from in brackets, e.g. "
    "[briefs/week-02.md]."
)


def config(llm) -> ConversationConfig:
    """The conversational config for `llm` (tests pass a scripted model). Explicit everywhere: left unset,
    the router and the built-in converse route have no model at all."""
    return ConversationConfig(
        system_prompt=SYSTEM,
        llm=llm,
        router=RouterConfig(
            llm=llm,
            prompt="Pick how to answer a question about KiranaCloud's competitors.",
            default_intent="converse",
            fallback_intent="converse",
        ),
        trace_scope="turn",  # each turn finishes its own trace; sessions continue by id
    )


CHAT_LLM = make_llm()


# Class-level @persist: the session's messages are saved after every step, so `handle_turn(...,
# session_id=<id>)` in a new process continues the conversation.
@persist()
@config(CHAT_LLM)
class AskAnalyst(Flow[ConversationState]):
    def __init__(self, **data: Any) -> None:
        # Not the Memory() every Flow builds for itself (OpenAI defaults); the chat only reads.
        data.setdefault("memory", analyst_slice())
        super().__init__(**data)

    @listen("ask_fact")
    def answer_fact(self) -> str:
        """A factual question about what a competitor did, where or when (answered from memory)."""
        question = self.state.current_user_message or ""
        # Shallow: a plain vector search (no LLM query analysis), ranked by similarity, recency, importance.
        matches = analyst_slice().recall(question, depth="shallow", limit=12)
        facts = [f"- {m.record.content} [{_source(m.record)}]" for m in matches]
        return self._answer("Facts from memory:\n" + ("\n".join(facts) or "(none)"))

    @listen("compare")
    def compare_competitors(self) -> str:
        """Compare competitors, or one competitor's threat level, across weeks."""
        rows = []
        for path in sorted(BRIEFS_DIR.glob("week-*.json")):
            if not re.fullmatch(r"week-\d\d\.json", path.name):
                continue
            brief = json.loads(path.read_text())
            for t in brief["threats"]:
                rows.append(
                    f"- week {brief['week']}, {t['company']}: {t['threat']}: {t['rationale']} "
                    f"[{path.with_suffix('.md')}]"
                )
        return self._answer("Threat assessments from the published briefs:\n" + ("\n".join(rows) or "(none)"))

    @listen("what_changed")
    def diff_weeks(self) -> str:
        """What changed between two weeks (or since last week)."""
        published = sorted(
            int(m.group(1))
            for p in BRIEFS_DIR.glob("week-*.md")
            if (m := re.fullmatch(r"week-(\d\d)\.md", p.name))
        )
        asked = [int(w) for w in re.findall(r"week\s*(\d+)", self.state.current_user_message or "", re.I)]
        weeks = sorted(set(asked))[-2:] if len(set(asked)) >= 2 else published[-2:]
        briefs = [
            f"=== briefs/week-{w:02d}.md ===\n{(BRIEFS_DIR / f'week-{w:02d}.md').read_text()}"
            for w in weeks
            if (BRIEFS_DIR / f"week-{w:02d}.md").exists()
        ]
        return self._answer("The published briefs to compare:\n\n" + ("\n\n".join(briefs) or "(none)"))

    def _answer(self, context: str) -> str:
        """One model call: the system prompt with this turn's context, then the conversation so far."""
        assert self.conversational_config is not None and self.conversational_config.llm is not None
        llm = self.conversational_config.llm
        messages = [{"role": "system", "content": f"{SYSTEM}\n\n{context}"}, *self.conversation_messages]
        return str(llm.call(messages))


def _source(record) -> str:
    """Where a memory came from: the brief file for /brief facts, the signal id for /competitor facts."""
    meta = record.metadata or {}
    return meta.get("file") or meta.get("signal_id") or record.scope
