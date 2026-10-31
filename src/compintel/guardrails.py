"""Task guardrails (spec §12.2). A function guardrail is `(TaskOutput) -> (ok, value_or_error)`: on `False`
crewai sends the error text back to the agent and re-runs the task, up to `guardrail_max_retries`.

Function vs string: these are free, deterministic and can see more than the output (here: the run's real
signal ids). A string guardrail (in crew.jsonc) is an LLM call that judges the output text against a rule; it
only sees the output, so it suits judgement calls ("is this rumor stated as fact?"), not lookups.
"""

from __future__ import annotations

import json
import re
from typing import Any

from crewai import TaskOutput
from pydantic import ValidationError

from compintel.models import Brief

SIGNAL_ID = re.compile(r"\b(StockSathi|ShelfMitra|BahiBook)-(\d+)-(\d+)\b")
MAX_BRIEF_WORDS = 600

# Signal ids given to the current crew run, registered by the crew's before_kickoff callback. Module-level
# because a guardrail only receives the TaskOutput; one crew run at a time in this project.
_known_ids: set[str] = set()
_low_credibility: set[str] = set()  # ids below LOW_CREDIBILITY: may only appear under discounted_claims
LOW_CREDIBILITY = 0.5


def register_signals(inputs: dict[str, Any]) -> dict[str, Any]:
    """crew before_kickoff callback: remember this run's signal ids (and which are low-credibility) so the
    guardrails can check the brief against the real inputs."""
    signals = json.loads(inputs.get("signals") or "[]")
    _known_ids.clear()
    _known_ids.update(s["id"] for s in signals)
    _low_credibility.clear()
    _low_credibility.update(s["id"] for s in signals if s.get("credibility", 1.0) < LOW_CREDIBILITY)
    return inputs


def brief_from(task_output: TaskOutput) -> Brief:
    """The Brief in a TaskOutput: already parsed, or the raw JSON (code fences tolerated)."""
    if isinstance(task_output.pydantic, Brief):
        return task_output.pydantic
    raw = task_output.raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    return Brief.model_validate_json(raw)


def _citation_problems(brief: Brief) -> list[str]:
    problems = []
    for field in ("tl_dr", "discounted_claims"):
        for i, item in enumerate(getattr(brief, field)):
            if not SIGNAL_ID.search(item):
                problems.append(f"{field}[{i}] cites no signal id: {item!r}")
    for t in brief.threats:
        if not t.signal_ids:
            problems.append(f"threat for {t.company} has no signal_ids")
    for i, r in enumerate(brief.recommendations):
        if not r.signal_ids:
            problems.append(f"recommendations[{i}] has no signal_ids: {r.action!r}")

    cited = {m.group(0) for text in _texts(brief) for m in SIGNAL_ID.finditer(text)}
    cited |= {sid for t in brief.threats for sid in t.signal_ids}
    cited |= {sid for r in brief.recommendations for sid in r.signal_ids}
    for sid in sorted(cited):
        m = SIGNAL_ID.fullmatch(sid)
        if m is None:
            problems.append(f"malformed signal id {sid!r} (expected e.g. StockSathi-{brief.week}-1)")
        elif int(m.group(2)) > brief.week:
            problems.append(f"{sid} is from a future week")
        elif int(m.group(2)) == brief.week and _known_ids and sid not in _known_ids:
            problems.append(f"{sid} is not one of this week's signals")
    return problems


def _texts(brief: Brief) -> list[str]:
    return [
        brief.headline,
        *brief.tl_dr,
        *brief.since_last_week,
        *brief.watchlist,
        *brief.discounted_claims,
        *(t.rationale for t in brief.threats),
        *(r.action for r in brief.recommendations),
    ]


def every_claim_cites_signal(task_output: TaskOutput) -> tuple[bool, Any]:
    """Every TL;DR item and discounted claim cites a signal id; every threat and recommendation lists its
    signal_ids; every id cited is real (this week's input signals, or an earlier week)."""
    try:
        brief = brief_from(task_output)
    except ValidationError as e:
        return False, f"The output is not a valid Brief JSON object: {e.errors()[:3]}"
    problems = _citation_problems(brief)
    if problems:
        return False, "Fix these citation problems and return the whole Brief again: " + "; ".join(problems)
    # Hand back the parsed Brief: crewai keeps a returned TaskOutput as-is, so no Converter call follows.
    task_output.pydantic = brief
    task_output.raw = brief.model_dump_json()
    return True, task_output


def low_credibility_only_discounted(task_output: TaskOutput) -> tuple[bool, Any]:
    """Signals below 0.5 credibility appear under discounted_claims and nowhere else (spec §8.2's rule).

    A function, not the spec's LLM-string guardrail: the credibility scores are in the inputs, not in the
    brief, so an LLM judging the brief alone can't check them. Live, it failed a week-1 brief with no
    low-credibility signal at all, twice, and its feedback made the writer discount a 0.9-credibility fact.
    """
    try:
        brief = brief_from(task_output)
    except ValidationError as e:
        return False, f"The output is not a valid Brief JSON object: {e.errors()[:3]}"
    discounted = {m.group(0) for c in brief.discounted_claims for m in SIGNAL_ID.finditer(c)}
    elsewhere = {
        m.group(0)
        for text in [brief.headline, *brief.tl_dr, *(t.rationale for t in brief.threats)]
        for m in SIGNAL_ID.finditer(text)
    }
    elsewhere |= {sid for t in brief.threats for sid in t.signal_ids}
    elsewhere |= {sid for r in brief.recommendations for sid in r.signal_ids}
    problems = [
        f"{sid} (credibility < 0.5) is missing from discounted_claims"
        for sid in sorted(_low_credibility - discounted)
    ]
    problems += [
        f"{sid} (credibility < 0.5) is used as fact outside discounted_claims"
        for sid in sorted(_low_credibility & elsewhere)
    ]
    if problems:
        return False, "Fix and return the whole Brief again: " + "; ".join(problems)
    return True, task_output


def brief_length_ok(task_output: TaskOutput) -> tuple[bool, Any]:
    """The rendered brief stays under MAX_BRIEF_WORDS (spec §12.2; used on the deep-dive write_brief, M4)."""
    from compintel.tools.render_brief import render_markdown

    try:
        words = len(render_markdown(brief_from(task_output)).split())
    except ValidationError as e:
        return False, f"The output is not a valid Brief JSON object: {e.errors()[:3]}"
    if words > MAX_BRIEF_WORDS:
        return False, f"The brief renders to {words} words; cut it to at most {MAX_BRIEF_WORDS}."
    return True, task_output
