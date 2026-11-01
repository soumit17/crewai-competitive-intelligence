"""The Flow's unified Memory, configured for this project (spec §11.1).

Why configure it here: a Flow builds a `Memory()` for itself when none is given, and that one defaults to
OpenAI for both its analysis LLM (gpt-5.4-mini) and its embedder (decisions.md #3/#4). M6 needs memory for
`@human_feedback(learn=True)`: lessons distilled from reviewer feedback are saved here and applied to the next
draft before a human sees it. M7 is the memory deep dive (scopes, slices, Ask the Analyst).
"""

from __future__ import annotations

from pathlib import Path

from crewai.memory.unified_memory import Memory

from compintel.config import embedder_config, get_settings, make_llm

HITL_SOURCE = "hitl"  # @human_feedback's learn_source default: the provenance tag on every lesson


def memory_dir() -> Path:
    return get_settings().p3_runs_dir / "memory"


def make_memory() -> Memory:
    # LLM: field inference on save (scope, categories, importance) and deep recall. Embedder: local ONNX.
    # Storage: LanceDB under runs/, so lessons outlive the process (week 2 runs in a new one).
    return Memory(llm=make_llm(), embedder=embedder_config(), storage=str(memory_dir()))


def hitl_lessons(memory: Memory, query: str, limit: int = 5) -> list[str]:
    """The reviewer's lessons relevant to `query`. recall(source=...) does *not* filter by source in crewai
    1.15.33 (it only decides which private records are visible), so the filter is done here."""
    matches = memory.recall(query, source=HITL_SOURCE, depth="shallow", limit=limit * 2)
    return [m.record.content for m in matches if m.record.source == HITL_SOURCE][:limit]
