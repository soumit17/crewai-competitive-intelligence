"""Compatibility shims for the pinned crewai version. Each one names the upstream gap and how to retire it.

Native structured outputs for Claude Haiku 5.5 (decided with the developer, M4):
crewai 1.15.33's Anthropic provider sends a response schema natively (constrained decoding) only for models
in `NATIVE_STRUCTURED_OUTPUT_MODELS`; Haiku 5.5 isn't listed, so crewai falls back to a forced
`structured_output` tool. On nested models that fallback is unreliable: in a live deep dive every
`StepObservation` (agent planning's per-step check) came back wrapped in an extra key, and
`ThreatAssessments.threats` came back as a JSON string. A live probe showed Haiku 5.5 accepts the native path
crewai already uses for Sonnet 5, so we add its prefix.
Retire: delete this when `test_upstream_still_lacks_haiku_5` fails (crewai lists it itself).
"""

from __future__ import annotations

HAIKU_5_PREFIX = "claude-haiku-5"
UPSTREAM_HAS_HAIKU_5: bool | None = None  # recorded before patching, for the retirement test


def register_native_structured_outputs() -> None:
    """Idempotent. Called by config.make_llm, the one place every LLM is built."""
    global UPSTREAM_HAS_HAIKU_5
    from crewai.llms.providers.anthropic import completion

    models = completion.NATIVE_STRUCTURED_OUTPUT_MODELS
    if UPSTREAM_HAS_HAIKU_5 is None:
        UPSTREAM_HAS_HAIKU_5 = HAIKU_5_PREFIX in models
    if HAIKU_5_PREFIX not in models:
        # A module global read at call time by `_supports_native_structured_outputs`.
        completion.NATIVE_STRUCTURED_OUTPUT_MODELS = (*models, HAIKU_5_PREFIX)  # type: ignore[assignment]
