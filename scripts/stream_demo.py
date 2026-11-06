"""Streaming below the Flow (spec §15): direct `LLM.call` with stream=True, and what the chunk events carry.

Three Haiku calls, well under $0.01 together:
  1. plain text           → LLMStreamChunkEvent(chunk="...") a few tokens at a time
  2. a tool call          → chunks with `tool_call` (the name first, then the arguments as they're generated);
                            crewai runs the function and makes the follow-up call, which streams too
  3. a structured answer  → the JSON of a Pydantic model, streamed, then validated

Run: uv run python scripts/stream_demo.py
"""

from __future__ import annotations

import sys
import threading
from collections import Counter
from typing import Any

from compintel.runtime import bootstrap


def gst_rate(item: str) -> str:
    """A stand-in tool the model can call."""
    return {"rice": "5%", "soap": "18%"}.get(item.lower(), "12%")


GST_TOOL = {
    "type": "function",
    "function": {
        "name": "gst_rate",
        "description": "The GST rate for an item sold in a kirana shop.",
        "parameters": {
            "type": "object",
            "properties": {"item": {"type": "string", "description": "e.g. rice, soap"}},
            "required": ["item"],
        },
    },
}


def demo(llm: Any, write: Any = sys.stdout.write) -> Counter:
    """Make the three calls on `llm` (stream=True), echoing chunk events; returns what kinds of chunk came."""
    from crewai.events.event_bus import crewai_event_bus
    from crewai.events.types.llm_events import LLMStreamChunkEvent

    from compintel.models import TieBreak

    seen: Counter = Counter()
    lock = threading.Lock()

    def on_chunk(source: Any, event: LLMStreamChunkEvent) -> None:
        with lock:  # handlers run on the bus's thread pool
            if event.tool_call is not None:
                seen["tool_call"] += 1
                fn = event.tool_call.function
                if fn.name and not fn.arguments:
                    write(f"\n  ⇢ tool_call chunk: {fn.name}(…)\n")
            else:
                seen["text"] += 1
                write(event.chunk)

    # Not crewai_event_bus.scoped_handlers(): that unregisters *every* handler while it lasts, the cost
    # listener included, so these calls would be missing from the ledger and the budget gate.
    handler = crewai_event_bus.on(LLMStreamChunkEvent)(on_chunk)
    try:
        write("\n1. text\n")
        llm.call("In two short sentences: why does GST e-invoicing matter to a kirana shop?")
        crewai_event_bus.flush()

        write("\n\n2. a tool call\n")
        answer = llm.call(
            "What GST rate applies to soap? Use the tool, then answer in one sentence.",
            tools=[GST_TOOL],
            available_functions={"gst_rate": gst_rate},
        )
        crewai_event_bus.flush()
        write(f"\n  → returned: {answer!r}\n")

        write("\n3. structured output (TieBreak)\n")
        out = llm.call(
            "A competitor cut prices 40% in our main city. Route this week as routine or material?",
            response_model=TieBreak,
        )
        crewai_event_bus.flush()
        write(f"\n  → validated: {out!r}\n")
    finally:
        crewai_event_bus.off(LLMStreamChunkEvent, handler)
    return seen


def main() -> None:
    bootstrap()
    from compintel.config import make_llm, stream_tokens
    from compintel.cost import get_ledger

    stream_tokens(True)
    before = get_ledger().spent_usd
    seen = demo(make_llm())
    spent = get_ledger().spent_usd - before
    print(f"\n\nchunk events: {dict(seen)}   cost ${spent:.6f}   P3 total ${get_ledger().spent_usd:.4f}")


if __name__ == "__main__":
    main()
