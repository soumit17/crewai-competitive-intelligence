"""Append-only cost ledger (`runs/costs.jsonl`, same row shape as P1/P2) and the $12 cap.

Rows are written by the event listener in listeners.py, so every LLM call anywhere (agent, crew, flow,
memory, guardrail) is counted without the caller doing anything. The cap is enforced by the
`budget_gate` hook in hooks.py, before each model call.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from compintel.config import get_settings

PROJECT = "p3"

# USD per million tokens. Source: https://platform.claude.com/docs/en/about-claude/pricing (read 2026-10-30).
# Haiku 5.5 uses the <=100k-token prompt tier; every call in this project stays under it (spec §21).
PRICES: dict[str, dict[str, float]] = {
    "claude-haiku-5-5": {"input": 0.10, "cache_write": 0.125, "cache_read": 0.01, "output": 0.50},
    "claude-sonnet-5-5": {"input": 2.00, "cache_write": 2.50, "cache_read": 0.20, "output": 10.00},
}


def split_usage(usage: dict[str, Any]) -> dict[str, int]:
    """CrewAI's usage dict counts cache reads/writes *inside* input_tokens; separate them so each is
    priced once at its own rate."""
    cache_read = int(usage.get("cached_prompt_tokens") or 0)
    cache_write = int(usage.get("cache_creation_tokens") or 0)
    return {
        "input": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0) - cache_read - cache_write,
        "cache_write": cache_write,
        "cache_read": cache_read,
        "output": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
    }


def price(model: str, usage: dict[str, Any]) -> float:
    # Events carry "claude-haiku-5-5" (prefix stripped by the provider); be lenient about "anthropic/".
    model = model.removeprefix("anthropic/")
    key = next((k for k in PRICES if model == k or model.startswith(k + "-")), None)
    if key is None:
        raise KeyError(f"no price for model {model!r}; add it to compintel.cost.PRICES")
    tokens = split_usage(usage)
    return sum(tokens[k] * PRICES[key][k] for k in tokens) / 1_000_000


class CostLedger:
    """Owns the ledger file. Thread-safe: crewai runs event handlers on a thread pool."""

    def __init__(self, path: Path, cap_usd: float) -> None:
        self.path = path
        self.cap_usd = cap_usd
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._spent = 0.0
        if path.exists():
            for line in path.read_text().splitlines():
                row = json.loads(line)
                self._spent += row["cost_usd"]
                if row.get("response_id"):
                    self._seen.add(row["response_id"])

    @property
    def spent_usd(self) -> float:
        return self._spent

    @property
    def over_cap(self) -> bool:
        return self._spent >= self.cap_usd

    def record(
        self, model: str, usage: dict[str, Any], response_id: str | None = None, agent: str | None = None
    ) -> float:
        # A replayed/resumed run can re-emit a response it already paid for; count each id once.
        if response_id and response_id in self._seen:
            return 0.0
        cost = price(model, usage)
        tokens = split_usage(usage)
        row = {
            "ts": datetime.now(UTC).isoformat(),
            "project": PROJECT,
            "model": model,
            "agent": agent,
            "input_tokens": tokens["input"],
            "cache_write_tokens": tokens["cache_write"],
            "cache_read_tokens": tokens["cache_read"],
            "output_tokens": tokens["output"],
            "cost_usd": round(cost, 6),
            "response_id": response_id,
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(row) + "\n")
            self._spent += cost
            if response_id:
                self._seen.add(response_id)
        return cost


@lru_cache
def get_ledger() -> CostLedger:
    s = get_settings()
    return CostLedger(s.p3_runs_dir / "costs.jsonl", s.p3_spend_cap_usd)
