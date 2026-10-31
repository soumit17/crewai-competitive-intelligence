"""LLMs for crews defined in JSON (crew.jsonc / agents/*.jsonc).

An agent file says `"llm": {"python": "compintel.llms.analyst"}`; crewai's JSON loader imports that attribute.
This module builds it on access (PEP 562 module `__getattr__`) from config.make_llm, so the model, max_tokens
and the Anthropic-only rule still live in config.py, not in JSON.

`scope(...)` swaps what the names return. It must wrap the *load*, not just the run: crewai binds a task's
string guardrail to the agent's LLM when the Task is constructed.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_final = False
_fake: Any = None


def __getattr__(name: str) -> Any:
    from compintel.config import LONG_OUTPUT_MAX_TOKENS, make_llm

    if name not in ("analyst", "writer"):
        raise AttributeError(name)
    if _fake is not None:
        return _fake
    if name == "writer":
        # Spec §7: the Brief Writer moves to MODEL_FINAL (Sonnet) only on --final. It writes a whole Brief.
        return make_llm(final=_final, max_tokens=LONG_OUTPUT_MAX_TOKENS)
    return make_llm()


@contextmanager
def scope(final: bool = False, fake: Any = None) -> Iterator[None]:
    global _final, _fake
    previous = (_final, _fake)
    _final, _fake = final, fake
    try:
        yield
    finally:
        _final, _fake = previous
