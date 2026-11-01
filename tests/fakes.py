"""$0 stand-ins for the model and the embedder (spec §16.1).

ScriptedLLM implements crewai's custom-LLM interface (subclass `BaseLLM`, implement `call`). It walks the same
steps as the native Anthropic provider (call context → started event → before-call hooks → completed event
with usage), so listeners, the cost ledger and `@on` hooks see exactly what they would in a live run.

Replies: text, structured (`response_model`), or tool calls (a list of `ToolCall`), returned as Anthropic
`tool_use` blocks, the shape the native provider hands crewai's agent loop, which then runs the tools.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
from anthropic.types import ToolUseBlock
from chromadb.api.types import EmbeddingFunction as ChromaEmbeddingFunction
from crewai import BaseLLM
from crewai.events.types.llm_events import LLMCallType
from crewai.hooks.dispatch import HookAborted
from crewai.llms.base_llm import LLMCallBlockedError, llm_call_context
from crewai.rag.core.types import Documents, Embeddings
from crewai.rag.embeddings.providers.custom.embedding_callable import CustomEmbeddingFunction
from pydantic import BaseModel, PrivateAttr

DEFAULT_USAGE = {
    "input_tokens": 1000,
    "output_tokens": 100,
    "cached_prompt_tokens": 0,
    "cache_creation_tokens": 0,
}


@dataclass
class ToolCall:
    name: str  # the sanitized tool name the LLM sees, e.g. "pricing_parse", "read_website_content"
    args: dict[str, Any]


@dataclass
class Rule:
    """First matching rule answers. `None` matches anything.

    reply: str (text), dict or BaseModel (structured; validated against the call's response_model),
    list[ToolCall], or a callable(messages, response_model) -> any of those.
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
        # BaseLLM validates `model` before field defaults apply.
        kwargs.setdefault("model", "claude-haiku-5-5")
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
                self.calls.append(
                    {
                        "role": role,
                        "messages": messages,
                        "response_model": response_model,
                        "tools": [_tool_name(t) for t in tools or []],
                    }
                )
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
            if isinstance(reply, list) and reply and isinstance(reply[0], ToolCall):
                n = len(self.calls)
                return [
                    ToolUseBlock(id=f"toolu_{n}_{k}", name=c.name, input=c.args, type="tool_use")
                    for k, c in enumerate(reply)
                ]
            if response_model is not None and isinstance(reply, dict | BaseModel):
                data = reply.model_dump() if isinstance(reply, BaseModel) else reply
                return response_model.model_validate(data)
            return reply
        raise AssertionError(f"ScriptedLLM: no rule for role={role!r}; prompt starts {prompt[:300]!r}")

    def supports_function_calling(self) -> bool:
        return True  # like the native Anthropic provider: crewai sends tool schemas, not ReAct text

    def get_context_window_size(self) -> int:
        return 200_000


def _tool_name(tool: Any) -> str:
    """Tool schemas arrive OpenAI-style ({"function": {"name": ...}}) or flat ({"name": ...})."""
    if isinstance(tool, dict):
        return tool.get("function", tool).get("name", "?")
    return getattr(tool, "name", "?")


def tool_results(messages: list[Any]) -> str:
    """All tool outputs the agent loop has fed back so far, for rules that react to what a tool returned."""
    return json.dumps([m for m in messages if isinstance(m, dict) and m.get("role") == "tool"], default=str)


class FakeEmbedder(CustomEmbeddingFunction, ChromaEmbeddingFunction):
    """Deterministic hash vectors: same text → same vector; different texts → near-orthogonal.

    crewai's "custom" embedder provider takes the *class* and instantiates it with no arguments. Two
    validators check it: KnowledgeStorage against chromadb's EmbeddingFunction, the provider against crewai's
    CustomEmbeddingFunction (not a subclass of chromadb's in 1.15.33), so it subclasses both.
    conftest makes it every test's embedder, so no test can trigger the real ONNX model download.
    """

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def __call__(self, input: Documents) -> Embeddings:  # noqa: A002 - chromadb's parameter name
        return [np.array(self._embed(t), dtype=np.float32) for t in input]

    @staticmethod
    def name() -> str:
        return "compintel-fake"

    def get_config(self) -> dict[str, Any]:
        return {"dim": self.dim}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> FakeEmbedder:
        return FakeEmbedder(**config)

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in text.lower().split():
            h = int.from_bytes(hashlib.sha256(token.encode()).digest()[:8], "big")
            vec[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


FAKE_EMBEDDER_SPEC: dict[str, Any] = {"provider": "custom", "config": {"embedding_callable": FakeEmbedder}}


def text_reply(answer: str) -> Callable[..., str]:
    """ReAct-style final answer, for agents run without a response_model."""
    return lambda *_: f"Thought: I know the answer.\nFinal Answer: {answer}"
