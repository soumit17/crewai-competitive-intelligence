"""`@on` execution hooks (spec §12.1). M0: budget gate. M2: URL allowlist. M3: file allowlist.
M6: render gate. M8: iteration guard, secret redaction, step tracer, input defaults (and the deep dive's
crew-scoped hook, in deep_dive_crew.py).

Hooks *control* (listeners only observe). Only `HookAborted` stops a run: any other exception raised in
a hook is swallowed (fail-open), so a buggy hook can't crash a run but also can't protect it.

What a hook's return value means depends on the point: at POST_MODEL_CALL a string replaces the model's
response; at the step and input points any non-None value replaces the payload (the task output, the
inputs). Hooks that only look must return None.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from crewai.events.event_bus import crewai_event_bus
from crewai.hooks import HookAborted, InterceptionPoint, on
from crewai.hooks.dispatch import get_hooks

from compintel.config import get_settings
from compintel.cost import get_ledger
from compintel.world.story import WEEKS

# crewai sanitizes tool names before matching `tools=` filters: "Read website content" -> this.
SCRAPE_TOOL = "read_website_content"
FILE_READ_TOOL = "read_a_files_content"
RENDER_TOOL = "render_brief"  # crewai-tools' FileReadTool ("Read a file's content")
READABLE_DIR = Path("briefs")  # cwd-relative, like the briefs the crews write


def budget_gate(ctx) -> None:
    """Refuse every model call once the project has spent its cap.

    PRE_MODEL_CALL rather than the spec's EXECUTION_START: it also covers `Agent.kickoff` (no crew/flow
    execution boundary) and stops a run that crosses the cap midway, not only at its start.
    """
    # The ledger is fed by a listener on a thread pool; wait for in-flight rows so the total is current.
    crewai_event_bus.flush()
    ledger = get_ledger()
    if ledger.over_cap:
        raise HookAborted(
            reason=f"P3 spend cap reached: ${ledger.spent_usd:.4f} of ${ledger.cap_usd:.2f}",
            source="budget-gate",
        )


def url_allowlist(ctx) -> None:
    """The network policy for LLM-chosen URLs: only the fake web server.

    crewai-tools' own SSRF guard blocks 127.0.0.1, so compintel runs switch it off
    (CREWAI_TOOLS_ALLOW_UNSAFE_PATHS, see config.apply_env). This hook takes its place, and is stricter:
    one host:port instead of "any public address".
    """
    allowed = urlparse(get_settings().fakeweb_url)
    raw = str((ctx.tool_input or {}).get("website_url") or "")
    url = urlparse(raw)
    ok = (
        url.scheme in ("http", "https")
        and url.username is None  # "http://127.0.0.1:8765@evil.example" has hostname evil.example
        and (url.hostname, url.port) == (allowed.hostname, allowed.port)
    )
    if not ok:
        raise HookAborted(reason=f"URL not allowed: {raw!r} (only {allowed.netloc})", source="url-allowlist")


def file_read_allowlist(ctx) -> None:
    """The file policy for LLM-chosen paths: only files under briefs/.

    Same story as url_allowlist: the escape hatch that lets the scraper reach 127.0.0.1 also lifts
    FileReadTool's own base_dir containment, so this hook takes its place. Paths are resolved first, so
    "briefs/../.env" and symlinks out of briefs/ are caught.
    """
    raw = str((ctx.tool_input or {}).get("file_path") or "")
    allowed = READABLE_DIR.resolve()
    target = Path(raw).resolve() if raw else None
    if target is None or not target.is_relative_to(allowed):
        raise HookAborted(
            reason=f"File not allowed: {raw!r} (only under {READABLE_DIR}/)", source="file-allowlist"
        )


def render_gate(ctx) -> None:
    """Hook-based HITL (spec §10.3, the third pattern): ask a human before the writer renders the brief.

    Unlike @human_feedback it sits inside a crew's agent loop, can only say yes or no (no routing, no
    lessons, no async pause), and blocks the terminal. Opt-in: `compintel brief --render-gate`.

    A refusal blocks that one tool call, it doesn't stop the crew: render_brief is `result_as_answer`, so
    crewai makes the block message ("Tool execution blocked by hook...") the task's final answer, and the
    render task's output file gets that text. Check with `render_refused()`.
    """
    week = (ctx.tool_input or {}).get("week", "?")
    answer = ctx.request_human_input(
        prompt=f"The Brief Writer wants to render week {week}'s brief.",
        default_message="Press Enter to allow, or type 'no' to stop the crew:",
    )
    if answer.strip().lower() in ("no", "n", "stop"):
        raise HookAborted(reason=f"render of week {week} refused by the reviewer", source="render-gate")


def render_refused(markdown: str) -> bool:
    return markdown.startswith("Tool execution blocked by hook")


def install_render_gate() -> None:
    on(InterceptionPoint.PRE_TOOL_CALL, tools=[RENDER_TOOL])(render_gate)


def is_installed(hook) -> bool:
    """True if `hook` is registered globally (`@on` with filters registers `hook._registered_hook`)."""
    registered = [h for point in InterceptionPoint for h in get_hooks(point)]
    return hook in registered or getattr(hook, "_registered_hook", None) in registered


# ---------------------------------------------------------------------------------------------- M8
MAX_ITERATIONS = 12


def iteration_guard(ctx) -> None:
    """End a run whose agent loop passes 12 iterations.

    Our own agents stop at max_iter=8 (config.AGENT_LIMITS), so this is the backstop for agents crewai
    builds itself with its default max_iter=25: the generic manager of the `manager_llm` variant. A
    PRE_MODEL_CALL abort ends the run (not just one call), which is what a runaway loop needs.
    Direct LLM calls (the router, memory analysis, knowledge queries) report 0 iterations.
    """
    iterations = getattr(ctx, "iterations", 0) or 0
    if iterations > MAX_ITERATIONS:
        role = getattr(getattr(ctx, "agent", None), "role", None) or "?"
        raise HookAborted(
            reason=f"{role} is on iteration {iterations} (limit {MAX_ITERATIONS})", source="iteration-guard"
        )


REDACTED = "[REDACTED]"
# Key shapes, not just our key: a model can repeat any secret that reached its context (a scraped page, a
# tool error). Anthropic and OpenAI-style keys, AWS access key ids, bearer tokens.
SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}"),
]
SECRET_ENV_VARS = ("ANTHROPIC_API_KEY",)


def redact(text: str) -> str:
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name) or ""
        if len(value) >= 8:  # a too-short value would redact ordinary words
            text = text.replace(value, REDACTED)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact_secrets(ctx) -> str | None:
    """Scrub secrets from every model response before an agent acts on it or a brief/ledger/memory keeps it.

    POST_MODEL_CALL uses *replace* semantics: returning a string swaps the response (a structured answer
    arrives as its JSON and is re-validated afterwards). Returns None when there is nothing to scrub, so
    the dispatch is recorded as "proceeded", not "modified".
    """
    response = getattr(ctx, "response", None)
    if not isinstance(response, str):
        return None
    cleaned = redact(response)
    return cleaned if cleaned != response else None


class StepTrace:
    """Pairs PRE_STEP with POST_STEP to time every task and flow method; appends to runs/steps.jsonl.

    StepContext carries no timing, so the PRE hook stamps a start time, keyed by the step's owner (the task
    or flow object) and name. A step that never finishes (a paused review, a crash) leaves no row.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started: dict[tuple[str | None, str | None, int], float] = {}
        self.entries: list[dict[str, Any]] = []

    def reset(self) -> None:
        with self._lock:
            self._started.clear()
            self.entries.clear()

    @staticmethod
    def _key(ctx) -> tuple[str | None, str | None, int]:
        owner = ctx.task if ctx.task is not None else ctx.flow
        return (ctx.kind, ctx.step_name, id(owner))

    def pre_step(self, ctx) -> None:
        with self._lock:
            self._started[self._key(ctx)] = time.perf_counter()
        return None  # a non-None return would replace the step's input

    def post_step(self, ctx) -> None:
        with self._lock:
            started = self._started.pop(self._key(ctx), None)
        name = str(ctx.step_name or "?")
        row = {
            "ts": datetime.now(UTC).isoformat(),
            "kind": ctx.kind,  # "task" or "flow_method"
            "step": name if len(name) <= 80 else name[:77] + "...",  # unnamed tasks report their description
            "agent": ctx.agent_role,
            "flow_id": getattr(getattr(ctx.flow, "state", None), "id", None),
            "seconds": round(time.perf_counter() - started, 2) if started is not None else None,
        }
        log = get_settings().p3_runs_dir / "steps.jsonl"
        with self._lock:
            self.entries.append(row)
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a") as f:
                f.write(json.dumps(row) + "\n")
        return None  # a non-None return would replace the task output / method result


step_trace = StepTrace()


def step_tracer_start(ctx) -> None:
    return step_trace.pre_step(ctx)


def step_tracer(ctx) -> None:
    """POST_STEP: log ctx.kind, ctx.step_name and the duration (see StepTrace)."""
    return step_trace.post_step(ctx)


DEFAULT_CURRENCY = "INR"


def inputs_defaults(ctx) -> None:
    """INPUT: fill defaults and reject impossible weeks before anything runs (crews and flows alike).

    Mutates ctx.inputs in place (it is the same dict as the payload) and returns None. `currency` goes to
    crews only, where it is one more template variable; a Flow's inputs become fields of its typed state.
    Chat sessions have no week and pass through.

    A crew resuming from a checkpoint is left alone: its kickoff inputs are empty (the saved ones are
    restored separately), and crewai only skips re-interpolating the task templates *because* they are
    empty. One added key and it interpolates `{week}` from {"currency": "INR"}, and the resume fails.
    """
    if getattr(ctx.crew, "checkpoint_kickoff_event_id", None) is not None:
        return None
    inputs = ctx.inputs if isinstance(ctx.inputs, dict) else {}
    if ctx.crew is not None:
        inputs.setdefault("currency", DEFAULT_CURRENCY)
    week = inputs.get("week")
    if week is None:
        return None
    try:
        ok = int(week) in WEEKS
    except (TypeError, ValueError):
        ok = False
    if not ok:
        raise HookAborted(reason=f"week {week!r} is outside the mock world {WEEKS}", source="inputs-defaults")
    return None


def install() -> None:
    """Register the global hooks. Tests clear them with `clear_all_hooks()` after each test.

    Order within a point is registration order: the budget gate runs before the iteration guard.
    """
    # Twice on purpose: EXECUTION_START refuses to *start* a crew or flow over the cap (the spec's point);
    # PRE_MODEL_CALL also covers Agent.kickoff, which has no execution boundary, and a run crossing the cap.
    on(InterceptionPoint.EXECUTION_START)(budget_gate)
    on(InterceptionPoint.PRE_MODEL_CALL)(budget_gate)
    on(InterceptionPoint.PRE_MODEL_CALL)(iteration_guard)
    on(InterceptionPoint.POST_MODEL_CALL)(redact_secrets)
    # tools= scopes the hook: it only runs for the scrape tool, so other tool calls skip it entirely.
    on(InterceptionPoint.PRE_TOOL_CALL, tools=[SCRAPE_TOOL])(url_allowlist)
    on(InterceptionPoint.PRE_TOOL_CALL, tools=[FILE_READ_TOOL])(file_read_allowlist)
    on(InterceptionPoint.PRE_STEP)(step_tracer_start)
    on(InterceptionPoint.POST_STEP)(step_tracer)
    on(InterceptionPoint.INPUT)(inputs_defaults)
