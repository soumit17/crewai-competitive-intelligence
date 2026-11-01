"""Unified Memory for CompIntel (spec §11.1): one store, scoped views for each reader and writer.

Why configure it here: crewai's `Memory()` defaults to OpenAI for both its analysis LLM (gpt-5.4-mini) and its
embedder (decisions.md #3/#4), and every Flow builds one for itself when it has none.

The tree (kept shallow):
    /competitor/<company>   each week's signals about that competitor (written by the Flow's publish step)
    /brief/week-NN          facts from each published brief (also written at publish)
    /hitl                   lessons distilled from reviewer feedback (the Flow's own memory, learn=True)

Who sees what:
    scout          MemoryScope /competitor/<company>      read-only, its company only
    Brief Writer   MemorySlice /competitor + /brief + /hitl, read-only
    Flow           MemoryScope /hitl                       @human_feedback lessons and the pre-review
    Ask the Analyst MemorySlice /competitor + /brief, read-only

A scope is a view rooted at one path (paths are relative to it). A slice spans several paths and is read-only
by default; a write to a read-only slice is silently ignored (crewai 1.15.33: returns None, no error).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from crewai.memory.memory_scope import MemoryScope, MemorySlice
from crewai.memory.unified_memory import Memory

from compintel.config import embedder_config, get_settings, make_llm

HITL_SOURCE = "hitl"  # @human_feedback's learn_source default: the provenance tag on every lesson
HITL_SCOPE = "/hitl"
COMPETITOR_ROOT = "/competitor"
BRIEF_ROOT = "/brief"


def memory_dir() -> Path:
    return get_settings().p3_runs_dir / "memory"


def make_memory() -> Memory:
    # LLM: field inference on save (scope, categories, importance), consolidation and deep recall.
    # Embedder: local ONNX. Storage: LanceDB under runs/, so memory outlives the process.
    # recency_half_life_days=14: last week's facts outrank week 1's when both match (spec §11.1).
    return Memory(
        llm=make_llm(), embedder=embedder_config(), storage=str(memory_dir()), recency_half_life_days=14
    )


@lru_cache
def _shared(path: str) -> Memory:
    return make_memory()


def shared() -> Memory:
    """The process's one Memory for the current runs dir: every view below is over the same store."""
    return _shared(str(memory_dir()))


def slug(company: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")


class ShallowScope(MemoryScope):
    """A MemoryScope whose recall is always `depth="shallow"`: for views handed to agents.

    crewai's agents recall with the default `depth="deep"`, which runs crewai's RecallFlow (plus an LLM query
    analysis per recall), and a RecallFlow joins the run's state with a field crew checkpoints can't serialize
    (a class): after one deep recall, every later checkpoint in the run failed (M7). Shallow is a plain vector
    search ranked by similarity, recency and importance, which is all these short, scoped stores need.
    """

    def recall(self, query: str, *args: Any, **kwargs: Any) -> Any:
        kwargs["depth"] = "shallow"
        return super().recall(query, *args, **kwargs)


class ReadOnlyScope(ShallowScope):
    """A scope an agent can only read: crewai gives it the recall tool but no save tool, and its kickoff
    save finds nothing to save.

    Why scouts don't write their own memories: an agent's automatic save after kickoff (an LLM extraction,
    then crewai's EncodingFlow) runs mid-week, and like RecallFlow the EncodingFlow joins the run's state, so
    every crew checkpoint after it failed (M7). The Flow records the scouts' signals itself at publish, after
    the crews, as structured facts (week, signal id, source) instead of LLM-extracted ones.
    """

    @property
    def read_only(self) -> bool:
        return True

    def extract_memories(self, content: str) -> list[str]:
        return []  # crewai calls this (an LLM call) before checking read_only; nothing to extract here


class ShallowSlice(MemorySlice):
    """The MemorySlice twin of ShallowScope."""

    def recall(self, query: str, *args: Any, **kwargs: Any) -> Any:
        kwargs["depth"] = "shallow"
        return super().recall(query, *args, **kwargs)


def scout_view(company: str) -> MemoryScope:
    return ReadOnlyScope(memory=shared(), root_path=f"{COMPETITOR_ROOT}/{slug(company)}")  # type: ignore[call-arg]


def competitor_scope(company: str) -> MemoryScope:
    return ShallowScope(memory=shared(), root_path=f"{COMPETITOR_ROOT}/{slug(company)}")  # type: ignore[call-arg]


def brief_scope(week: int) -> MemoryScope:
    return MemoryScope(memory=shared(), root_path=f"{BRIEF_ROOT}/week-{week:02d}")  # type: ignore[call-arg]


def hitl_scope() -> MemoryScope:
    # Shallow too: crewai's pre-review recalls with depth="deep" (an LLM query analysis per review, M6).
    return ShallowScope(memory=shared(), root_path=HITL_SCOPE)  # type: ignore[call-arg]


def writer_slice() -> MemorySlice:
    return ShallowSlice(memory=shared(), scopes=[COMPETITOR_ROOT, BRIEF_ROOT, HITL_SCOPE], read_only=True)  # type: ignore[call-arg]


def analyst_slice() -> MemorySlice:
    return MemorySlice(memory=shared(), scopes=[COMPETITOR_ROOT, BRIEF_ROOT], read_only=True)  # type: ignore[call-arg]


def hitl_lessons(memory: Memory | MemoryScope, query: str, limit: int = 5) -> list[str]:
    """The reviewer's lessons relevant to `query`. recall(source=...) does *not* filter by source in crewai
    1.15.33 (it only decides which private records are visible), so the filter is done here."""
    matches = memory.recall(query, source=HITL_SOURCE, depth="shallow", limit=limit * 2)
    return [m.record.content for m in matches if m.record.source == HITL_SOURCE][:limit]
