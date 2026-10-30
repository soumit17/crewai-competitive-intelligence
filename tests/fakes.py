"""$0 stand-ins for the model and the embedder (spec §16.1).

ScriptedLLM implements crewai's custom-LLM interface (subclass `BaseLLM`, implement `call`). It walks the same
steps as the native Anthropic provider (call context → started event → before-call hooks → completed event
with usage), so listeners, the cost ledger and `@on` hooks see exactly what they would in a live run.

M0 scope: text and structured (`response_model`) replies. Tool-call replies arrive with the tools in M2.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from crewai import BaseLLM
from crewai.events.types.llm_events import LLMCallType
from crewai.hooks.dispatch import HookAborted
from crewai.llms.base_llm import LLMCallBlockedError, llm_call_context
from pydantic import BaseModel, PrivateAttr

DEFAULT_USAGE = {
    "input_tokens": 1000,
    "output_tokens": 100,
    "cached_prompt_tokens": 0,
    "cache_creation_tokens": 0,
}


@dataclass
class Rule:
    """First matching rule answers. `None` matches anything.

    reply: str (text), dict or BaseModel (structured; validated against the call's response_model),
    or a callable(messages, response_model) -> any of those.
    """

    reply: Any
    role: str | None = None  # agent role, e.g. "Competitive Signal Scout"
    marker: str | None = None  # substring that must appear in the prompt, e.g. a task name
    times: int | None = None  # None = reusable; N = answer at most N calls


class ScriptedLLM(BaseLLM):
    """Deterministic fake LLM. Inspect `.calls` to assert what the framework sent."""

    # Priced like Haiku so the cost path is exercised for real (fake usage, tmp ledger).
    model: str = "claude-haiku-5-5"
    provider: str = "anthropic"
    usage: dict[str, int] = DEFAULT_USAGE

    _rules: list[Rule] = PrivateAttr(default_factory=list)
    _used: dict[int, int] = PrivateAttr(default_factory=dict)
    calls: list[dict[str, Any]] = []

    def __init__(self, *rules: Rule, **kwargs: Any) -> None:
        kwargs.setdefault(
            "model", "claude-haiku-5-5"
        )  # BaseLLM validates `model` before field defaults apply
        super().__init__(**kwargs)
        self._rules = list(rules)
        self.calls = []

    def call(
        self,
        messages: str | list[Any],
        tools: list[dict[str, Any]] | None = None,
        callbacks: list[Any] | None = None,
        available_functions: dict[str, Any] | None = None,
        from_task: Any | None = None,
        from_agent: Any | None = None,
        response_model: type[BaseModel] | None = None,
    ) -> Any:
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        with llm_call_context():
            try:
                self._emit_call_started_event(
                    messages, tools, callbacks, available_functions, from_task, from_agent
                )
                self._invoke_before_llm_call_hooks(messages, from_agent)
                role = getattr(from_agent, "role", None)
                self.calls.append({"role": role, "messages": messages, "response_model": response_model})
                reply = self._answer(role, messages, response_model)
            except (HookAborted, LLMCallBlockedError) as e:
                self._emit_call_denied_event(e, from_task, from_agent)
                raise
            self._track_token_usage_internal(dict(self.usage))
            self._emit_call_completed_event(
                response=reply,
                call_type=LLMCallType.LLM_CALL,
                from_task=from_task,
                from_agent=from_agent,
                messages=messages,
                usage=dict(self.usage),
                response_id=f"scripted-{id(self)}-{len(self.calls)}",
            )
            return reply

    def _answer(self, role: str | None, messages: list[Any], response_model: type[BaseModel] | None) -> Any:
        prompt = json.dumps(messages, default=str)
        for i, rule in enumerate(self._rules):
            if rule.role is not None and rule.role != role:
                continue
            if rule.marker is not None and rule.marker not in prompt:
                continue
            if rule.times is not None and self._used.get(i, 0) >= rule.times:
                continue
            self._used[i] = self._used.get(i, 0) + 1
            reply = rule.reply(messages, response_model) if callable(rule.reply) else rule.reply
            if response_model is not None and isinstance(reply, dict | BaseModel):
                data = reply.model_dump() if isinstance(reply, BaseModel) else reply
                return response_model.model_validate(data)
            return reply
        raise AssertionError(f"ScriptedLLM: no rule for role={role!r}; prompt starts {prompt[:300]!r}")

    def supports_function_calling(self) -> bool:
        return False

    def get_context_window_size(self) -> int:
        return 200_000


class FakeEmbedder:
    """Deterministic hash vectors in chromadb's EmbeddingFunction shape: texts -> unit vectors.

    Same text → same vector; different texts → near-orthogonal. Used for Memory/Knowledge from M4 on.
    """

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def __call__(self, input: list[str]) -> list[list[float]]:  # noqa: A002 - chromadb's parameter name
        return [self._embed(t) for t in input]

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in text.lower().split():
            h = int.from_bytes(hashlib.sha256(token.encode()).digest()[:8], "big")
            vec[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def text_reply(answer: str) -> Callable[..., str]:
    """ReAct-style final answer, for agents run without a response_model."""
    return lambda *_: f"Thought: I know the answer.\nFinal Answer: {answer}"
