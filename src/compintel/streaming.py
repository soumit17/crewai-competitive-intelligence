"""`compintel run --stream` (spec §15, M8b): watch a week as it happens, token by token.

crewai: with `flow.stream = True`, `Flow.kickoff()` returns a `StreamSession` instead of the result. The flow
runs on a background thread; iterating the session yields a `StreamFrame` for every event the run emits
(channels: llm, flow, tools, messages, lifecycle, custom) and `.result` holds the flow's return value at the
end. Token frames only appear if the LLMs themselves stream (`config.stream_tokens`).

`StreamPrinter` turns frames into terminal lines: flow steps, tasks, tool calls and the model's text as it
arrives. Several calls stream at once (the three scouts, the parallel analysts), so each switch of call gets
a short header naming the agent.
"""

from __future__ import annotations

import time
from typing import Any

from rich.console import Console
from rich.markup import escape

# Frames worth a line of their own; everything else (hooks, memory internals, ...) stays in the trace.
_FLOW_LINES = {"method_execution_started", "method_execution_finished", "method_execution_failed"}
_TASK_LINES = {"task_started", "task_failed", "crew_kickoff_started", "crew_kickoff_failed"}
_TOOL_LINES = {"tool_usage_started", "tool_usage_error", "tool_usage_finished"}


class StreamPrinter:
    """Callable on each StreamFrame; counts what it saw (tests, and the summary line)."""

    def __init__(self, console: Console | None = None, show_thinking: bool = False) -> None:
        self.console = console or Console(highlight=False)
        self.show_thinking = show_thinking
        self.tokens = 0  # text chunks printed
        self.tool_call_chunks = 0
        self.frames = 0
        self._call: str | None = None  # the call whose tokens are being printed
        self._mid_line = False
        self._started: dict[str, float] = {}

    def __call__(self, frame: Any) -> None:
        self.frames += 1
        data, kind = frame.data, frame.type
        if kind == "llm_stream_chunk":
            self._chunk(data)
        elif kind == "llm_thinking_chunk" and self.show_thinking:
            self._text(data, f"[dim]{escape(data.get('chunk', ''))}[/dim]", markup=True)
        elif kind in _FLOW_LINES:
            self._flow(kind, data)
        elif kind in _TASK_LINES:
            who = data.get("agent_role") or ""
            name = data.get("task_name") or data.get("crew_name") or ""
            mark = "[red]✗[/red]" if kind.endswith("failed") else "[cyan]•[/cyan]"
            self._line(f"  {mark} {escape(kind.replace('_', ' '))}: {escape(str(name)[:70])} {escape(who)}")
        elif kind in _TOOL_LINES:
            if kind == "tool_usage_started":
                self._line(f"    [magenta]→ tool[/magenta] {escape(str(data.get('tool_name')))}")
            elif kind == "tool_usage_error":
                tool, error = escape(str(data.get("tool_name"))), escape(str(data.get("error")))
                self._line(f"    [red]✗ tool {tool}: {error}[/red]")
        elif frame.channel == "lifecycle":
            self._line(f"[red]{escape(kind)}[/red] {escape(str(data.get('error', ''))[:200])}")

    def _chunk(self, data: dict[str, Any]) -> None:
        tool_call = data.get("tool_call")
        if tool_call:
            self.tool_call_chunks += 1
            name = (tool_call.get("function") or {}).get("name") or tool_call.get("name")
            if name and not (tool_call.get("function") or {}).get("arguments"):
                self._header(data)
                self._line(f"    [magenta]⇢ calls {escape(str(name))}(…)[/magenta]")
            return
        text = data.get("chunk") or ""
        if text:
            self.tokens += 1
            self._text(data, text, markup=False)

    def _text(self, data: dict[str, Any], text: str, markup: bool) -> None:
        self._header(data)
        self.console.print(text, end="", markup=markup, highlight=False, soft_wrap=True)
        self._mid_line = not text.endswith("\n")

    def _header(self, data: dict[str, Any]) -> None:
        call = data.get("call_id")
        if call == self._call:
            return
        who = data.get("agent_role") or "model"
        task = data.get("task_name") or ""
        self._line(f"    [bold blue]{escape(who)}[/bold blue] [dim]{escape(str(task)[:60])}[/dim]")
        self._call = call

    def _flow(self, kind: str, data: dict[str, Any]) -> None:
        method = str(data.get("method_name"))
        if kind == "method_execution_started":
            self._started[method] = time.perf_counter()
            self._line(f"[bold]▶ {escape(method)}[/bold]")
            return
        took = time.perf_counter() - self._started.pop(method, time.perf_counter())
        if kind == "method_execution_failed":
            self._line(f"[red]✗ {escape(method)}[/red] {escape(str(data.get('error', ''))[:200])}")
        else:
            self._line(f"[green]✓ {escape(method)}[/green] [dim]{took:.1f}s[/dim]")

    def _line(self, markup: str) -> None:
        if self._mid_line:
            self.console.print()
            self._mid_line = False
        self._call = None  # any other line ends the token run: the next token gets its header again
        self.console.print(markup, highlight=False)

    def finish(self) -> None:
        if self._mid_line:
            self.console.print()
            self._mid_line = False


def consume(session: Any, on_frame: Any) -> Any:
    """Read a StreamSession to the end, handing each frame to `on_frame`; returns the flow's result (raises
    the run's error, as kickoff would)."""
    try:
        for frame in session:
            on_frame(frame)
    finally:
        finish = getattr(on_frame, "finish", None)
        if finish is not None:
            finish()
    return session.result
