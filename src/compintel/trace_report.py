"""`compintel traces`: read runs back out of Phoenix and summarize what docs/observability.md asks for
(spec §15): manager delegations, async fan-out, guardrail retries, hook interventions, memory, the human
review, and per-agent token share ("where the money goes"). $0: it only reads the local Phoenix.

Works on spans as Phoenix's client returns them (`Client().spans.get_spans`): dicts with name, span_kind,
context.trace_id / span_id, parent_id, start_time / end_time (ISO), status_code / status_message and flat
`attributes`. tests/test_tracing.py builds the same dicts from an in-memory exporter.
"""

from __future__ import annotations

import contextlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from compintel.cost import price
from compintel.tracing import AGENT_ATTR

Span = dict[str, Any]
DELEGATION_TOOLS = ("delegate_work_to_coworker", "ask_question_to_coworker")


def _t(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _secs(span: Span) -> float:
    return (_t(span["end_time"]) - _t(span["start_time"])).total_seconds()


def _role(agent_span: Span) -> str:
    """'Head of Strategy.pricing_analysis.execute' → 'Head of Strategy'."""
    return agent_span["name"].split(".", 1)[0]


@dataclass
class Trace:
    root: Span
    spans: list[Span]
    by_id: dict[str, Span] = field(init=False)
    children: dict[str | None, list[Span]] = field(init=False)

    def __post_init__(self) -> None:
        self.by_id = {s["context"]["span_id"]: s for s in self.spans}
        self.children = defaultdict(list)
        for s in sorted(self.spans, key=lambda s: s["start_time"]):
            self.children[s.get("parent_id")].append(s)

    def ancestors(self, span: Span) -> list[Span]:
        out, parent = [], span.get("parent_id")
        while parent and parent in self.by_id:
            out.append(self.by_id[parent])
            parent = self.by_id[parent].get("parent_id")
        return out

    def kind(self, kind: str) -> list[Span]:
        return [s for s in self.spans if s["span_kind"] == kind]


def group_traces(spans: list[Span]) -> list[Trace]:
    """Spans → one Trace per trace id that has its root, oldest first."""
    by_trace: dict[str, list[Span]] = defaultdict(list)
    for s in spans:
        by_trace[s["context"]["trace_id"]].append(s)
    traces = []
    for members in by_trace.values():
        roots = [s for s in members if not s.get("parent_id")]
        if roots:
            traces.append(Trace(min(roots, key=lambda s: s["start_time"]), members))
    return sorted(traces, key=lambda t: t.root["start_time"])


# ------------------------------------------------------------------ the sections
def token_share(traces: list[Trace]) -> list[dict[str, Any]]:
    """LLM spans grouped by the agent that made the call (`compintel.agent`; '-' = no agent: memory, the
    router tie-break, the review classifier), with tokens and USD at our price table."""
    rows: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "prompt": 0, "completion": 0, "usd": 0.0}
    )
    for t in traces:
        for s in t.kind("LLM"):
            a = s["attributes"]
            prompt = int(a.get("llm.token_count.prompt") or 0)
            completion = int(a.get("llm.token_count.completion") or 0)
            cache_read = int(a.get("llm.token_count.prompt_details.cache_read") or 0)
            r = rows[a.get(AGENT_ATTR) or "-"]
            r["calls"] += 1
            r["prompt"] += prompt
            r["completion"] += completion
            model = a.get("llm.model_name")
            if model:
                usage = {
                    "input_tokens": prompt,
                    "output_tokens": completion,
                    "cached_prompt_tokens": cache_read,
                }
                with contextlib.suppress(KeyError):  # an unpriced model counts tokens, not dollars
                    r["usd"] += price(model, usage)
    total = sum(r["prompt"] + r["completion"] for r in rows.values()) or 1
    out = [{"agent": k, **v, "share": (v["prompt"] + v["completion"]) / total} for k, v in rows.items()]
    return sorted(out, key=lambda r: (-r["usd"], -r["share"]))


def delegations(t: Trace) -> list[dict[str, Any]]:
    """Manager → coworker, from crewai's delegation tool spans and the agent span nested under each."""
    out = []
    for tool in t.kind("TOOL"):
        if not tool["name"].startswith(DELEGATION_TOOLS):
            continue
        manager = next((_role(a) for a in t.ancestors(tool) if a["span_kind"] == "AGENT"), "?")
        worker = next(
            (_role(c) for c in t.children[tool["context"]["span_id"]] if c["span_kind"] == "AGENT"), "?"
        )
        out.append(
            {
                "manager": manager,
                "tool": tool["name"].split(".")[0],
                "coworker": worker,
                "seconds": _secs(tool),
            }
        )
    return out


def fan_out(t: Trace) -> list[list[Span]]:
    """Agent spans under the same parent that ran at the same time (crewai async tasks, parallel scouts)."""
    groups: list[list[Span]] = []
    for siblings in t.children.values():
        agents = [s for s in siblings if s["span_kind"] == "AGENT"]
        current: list[Span] = []
        end: datetime | None = None
        for s in agents:  # sorted by start
            if end is not None and _t(s["start_time"]) < end:
                current.append(s)
                end = max(end, _t(s["end_time"]))
            else:
                if len(current) > 1:
                    groups.append(current)
                current, end = [s], _t(s["end_time"])
        if len(current) > 1:
            groups.append(current)
    return groups


def guardrails(t: Trace) -> list[dict[str, Any]]:
    return [
        {
            "name": s["name"].removeprefix("guardrail ")[:60],
            "passed": s["status_code"] != "ERROR",
            "message": s.get("status_message") or "",
        }
        for s in t.kind("GUARDRAIL")
        if s["name"].startswith("guardrail")
    ]


def hook_spans(t: Trace) -> list[Span]:
    return [s for s in t.kind("GUARDRAIL") if s["name"].startswith("hook ")]


def memory_counts(t: Trace) -> dict[str, tuple[int, float]]:
    out: dict[str, tuple[int, float]] = {}
    for s in t.spans:
        if s["name"].startswith(("memory.", "knowledge.")):
            n, secs = out.get(s["name"], (0, 0.0))
            out[s["name"]] = (n + 1, secs + _secs(s))
    return out


def reviews(t: Trace) -> list[Span]:
    return [s for s in t.spans if s["name"].startswith("human review")]


# ------------------------------------------------------------------ markdown
def _timeline(group: list[Span], width: int = 40) -> list[str]:
    start = min(_t(s["start_time"]) for s in group)
    span_total = max((_t(s["end_time"]) - start).total_seconds() for s in group) or 1
    lines = []
    for s in group:
        a = (_t(s["start_time"]) - start).total_seconds()
        b = a + _secs(s)
        bar = " " * round(a / span_total * width) + "█" * max(1, round((b - a) / span_total * width))
        lines.append(f"{_role(s)[:22]:22} |{bar:<{width}}| {a:6.2f}s → {b:6.2f}s")
    return lines


def report(traces: list[Trace]) -> str:
    out: list[str] = []
    w = out.append
    w("| trace | root | started (UTC) | seconds | spans | LLM calls | tokens |")
    w("|---|---|---|---:|---:|---:|---:|")
    for t in traces:
        llm = t.kind("LLM")
        tokens = sum(int(s["attributes"].get("llm.token_count.total") or 0) for s in llm)
        tid = t.root["context"]["trace_id"]
        w(
            f"| `{tid[:8]}` | {t.root['name']} | {t.root['start_time'][:19]} | {_secs(t.root):.0f} | "
            f"{len(t.spans)} | {len(llm)} | {tokens:,} |"
        )

    w("\n### Where the money goes (per-agent token share)\n")
    w("| agent | LLM calls | prompt tok | output tok | share of tokens | USD |")
    w("|---|---:|---:|---:|---:|---:|")
    for r in token_share(traces):
        w(
            f"| {r['agent']} | {r['calls']} | {r['prompt']:,} | {r['completion']:,} | {r['share']:.0%} | "
            f"{r['usd']:.4f} |"
        )

    for t in traces:
        w(f"\n### `{t.root['context']['trace_id'][:8]}` {t.root['name']}\n")
        if d := delegations(t):
            w("**Manager delegations** (crewai's delegation tool → the coworker's agent span inside it):\n")
            for x in d:
                w(f"- {x['manager']} → `{x['tool']}` → **{x['coworker']}** ({x['seconds']:.1f}s)")
            w("")
        if groups := fan_out(t):
            w("**Fan-out** (agent spans that ran at the same time):\n")
            for g in groups:
                w("```text")
                out.extend(_timeline(g))
                w("```")
        if g := guardrails(t):
            failed = [x for x in g if not x["passed"]]
            w(f"**Guardrails**: {len(g)} checks, {len(failed)} failed → retried.")
            for x in failed:
                w(f"- ✗ `{x['name']}`: {x['message'][:160]}")
            w("")
        if h := hook_spans(t):
            w("**Hook interventions**:\n")
            for s in h:
                w(f"- `{s['name']}` {s.get('status_message') or ''}")
            w("")
        if m := memory_counts(t):
            w(
                "**Memory and knowledge**: "
                + ", ".join(f"{k} ×{n} ({secs:.1f}s)" for k, (n, secs) in sorted(m.items()))
            )
            w("")
        for s in reviews(t):
            a = s["attributes"]
            answer = a.get("output.value", "")
            w(f"**Human review**: waited {_secs(s):.0f}s for the reviewer; answer {answer!r}")
            w("")
        if slow := sorted((s for s in t.spans if s is not t.root), key=_secs, reverse=True)[:5]:
            w("**Slowest spans**: " + ", ".join(f"{s['name'][:50]} {_secs(s):.1f}s" for s in slow))
    return "\n".join(out) + "\n"


START, END = "<!-- trace-report:start -->", "<!-- trace-report:end -->"


def write_into(path: Any, markdown: str) -> None:
    """Replace the text between the trace-report markers in `path` (the generated part of the doc)."""
    text = path.read_text()
    if START not in text or END not in text:
        raise ValueError(f"{path} has no {START} … {END} block")
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    path.write_text(f"{head}{START}\n{markdown}{END}{tail}")


def fetch(url: str, project: str, trace_ids: list[str] | None = None, limit: int = 20_000) -> list[Span]:
    from phoenix.client import Client

    client = Client(base_url=url)
    spans = client.spans.get_spans(project_identifier=project, trace_ids=trace_ids or None, limit=limit)
    return [dict(s) for s in spans]
