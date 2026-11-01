"""CompetitiveIntelFlow (spec §9): one week, end to end. The Flow owns state and routing; crews do the fuzzy
parts.

    load_week ─┐                      (+ load_knowledge_digest, a parallel @start)
               └ scout_all ─ and_ ─ assess_materiality (@router)
                                      ├ "nothing_new" → quiet_week ──────────────────────────────┐
                                      ├ "routine"     → run_short_update ┬ render_draft             │
                                      └ "material"    → run_deep_dive ───┘    ↓                      │
                       ┌──────── "revised" ── revise_brief ←"revise"─ editorial_review (@human_feedback)
                       └→ editorial_review          "revision_limit" ─┤  "approve" ───────────────→ publish
                                                                       └─────────────────────────→ publish
                                                       "reject" → archive

M6: a human reviews every crew-written brief before it's published (spec §10). M7 adds memory beyond lessons.

Week over week: `@persist` saves the state after every method (SQLite, crewai's default). Next week's run is a
*fork* of this one: `kickoff(restore_from_state_id=<last week's state.id>)` hydrates the state (history, last
week's brief) under a fresh `state.id`, so each week is its own execution and last week's record is untouched.

This module is also what crewai's CLI runs (`kickoff` / `plot` scripts in pyproject.toml). `crewai run
--inputs`
imports the kickoff script's module and calls the one Flow class *defined in it* directly, and a Flow subclass
loses its inherited @start/@listen graph in crewai 1.15.33, so the class and the entry points live together
here (the spec's separate main.py can't work), and `kickoff` itself does the bootstrap and the fork.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Literal

from crewai.flow.flow import Flow, and_, listen, or_, router, start
from crewai.flow.flow_config import flow_config
from crewai.flow.human_feedback import HumanFeedbackResult, human_feedback
from crewai.flow.persistence import persist

from compintel import materiality, review
from compintel.config import LONG_OUTPUT_MAX_TOKENS, PROJECT_ROOT, get_settings, make_llm
from compintel.memory import brief_scope, competitor_scope, hitl_lessons, hitl_scope
from compintel.models import Brief, CIState, ThreatAssessment, TieBreak, WeekSummary
from compintel.runtime import bootstrap
from compintel.tools.render_brief import render_markdown
from compintel.world.story import COMPANIES, WEEKS

BRIEFS_DIR = Path("briefs")  # cwd-relative, like the crews' drafts
MAX_REVISIONS = 2  # spec §9.1: a third "revise" publishes with an editor's note instead of looping forever
# The model behind the review: maps free-text feedback to approve/revise/reject, distills lessons, and
# pre-reviews the next draft with them (it rewrites a whole Brief, hence the long-output budget). Explicit:
# left unset, crewai would build one from $MODEL, or fall back to OpenAI.
REVIEW_LLM = make_llm(max_tokens=LONG_OUTPUT_MAX_TOKENS)


def state_index_path() -> Path:
    """{"published": {week: state.id}, "in_progress": {week: state.id}}: what next week forks from, and what a
    crashed week resumes from."""
    return get_settings().p3_runs_dir / "state_index.json"


def read_state_index() -> dict[str, dict[str, str]]:
    path = state_index_path()
    index = json.loads(path.read_text()) if path.exists() else {}
    return {"published": index.get("published", {}), "in_progress": index.get("in_progress", {})}


def _update_index(section: str, week: int, state_id: str | None) -> None:
    index = read_state_index()
    if state_id is None:
        index[section].pop(str(week), None)
    else:
        index[section][str(week)] = state_id
    state_index_path().parent.mkdir(parents=True, exist_ok=True)
    state_index_path().write_text(json.dumps(index, indent=2))


@persist()  # class-level: state saved after every method, default SQLiteFlowPersistence (crewai's data dir)
class CompetitiveIntelFlow(Flow[CIState]):
    def __init__(self, **data: Any) -> None:
        # The Flow's memory is the /hitl scope of our shared Memory (not the OpenAI-default one a Flow builds
        # itself): @human_feedback(learn=True) saves lessons into it and pre-reviews drafts from it, so brief
        # facts (/brief) can never be applied as "lessons".
        data.setdefault("memory", hitl_scope())
        super().__init__(**data)

    def kickoff(self, inputs: dict[str, Any] | None = None, **kwargs: Any) -> Any:  # type: ignore[override]
        """Bootstrap (hooks and cost listener; scouts and crews refuse to run without them), and for weeks
        after
        the first, fork last week's published state unless a `restore_from_state_id` is given."""
        bootstrap()
        if flow_config.hitl_provider is None:
            review.use(review.ConsoleReview())  # shows the rendered brief, not the Brief's repr
        if kwargs.get("from_checkpoint") is not None:  # crewai's checkpoint restore path: nothing to add
            return super().kickoff(inputs=inputs, **kwargs)
        inputs = dict(inputs or {})
        week = int(inputs.get("week", 1))
        if week > 1 and kwargs.get("restore_from_state_id") is None:
            previous = read_state_index()["published"].get(str(week - 1))
            if previous is None:
                raise ValueError(f"no published state for week {week - 1}; run week {week - 1} first")
            kwargs["restore_from_state_id"] = previous
        if kwargs.get("restore_from_state_id"):
            inputs.setdefault("previous_state_id", kwargs["restore_from_state_id"])
        return super().kickoff(inputs=inputs, **kwargs)

    # ---------------------------------------------------------------- starts (run concurrently)
    @start()
    def load_week(self) -> None:
        """Start a new week. If this run was forked from last week, move last week's result into history."""
        s = self.state
        if s.week not in WEEKS:
            raise ValueError(f"week must be one of {WEEKS}")
        if s.brief is not None and s.brief.week != s.week:
            # Hydrated from last week's state (fork): keep its outcome, reset the per-week fields.
            s.history.append(
                WeekSummary(
                    week=s.brief.week,
                    route=s.route,
                    headline=s.brief.headline,
                    threats={t.company: t.threat for t in s.brief.threats},
                    published_path=s.published_path,
                )
            )
            s.reports, s.route, s.brief, s.brief_markdown, s.published_path = {}, "", None, "", None
            s.materiality_score, s.materiality_reasons, s.revision_count, s.feedback_log = 0.0, [], 0, []
        if s.week > 1 and (not s.history or s.history[-1].week != s.week - 1):
            # restore_from_state_id falls back *silently* to a fresh state when the id isn't found: say so
            # here.
            raise ValueError(
                f"week {s.week} needs week {s.week - 1}'s state (fork with restore_from_state_id); "
                f"history has weeks {[h.week for h in s.history]}"
            )
        # What `run_week(week, resume=True)` forks from if this run dies before publishing.
        _update_index("in_progress", s.week, s.id)

    @start()
    def load_knowledge_digest(self) -> None:
        """A cheap, LLM-free digest of our own docs, run in parallel with load_week (spec §9.1 and_
        exercise)."""
        text = (get_settings().p3_knowledge_dir / "kiranacloud_pricing.md").read_text()
        match = re.search(r"Dukaan[^|]*\|\s*₹([\d,]+)", text)
        self.state.our_dukaan_price = int(match.group(1).replace(",", "")) if match else None

    # ---------------------------------------------------------------- scouting
    @listen(load_week)
    async def scout_all(self) -> None:
        # An async method: the three scouts run concurrently on the flow's event loop (Agent.kickoff_async).
        from compintel import scouting

        if self.state.reports and all(r.week == self.state.week for r in self.state.reports.values()):
            return  # resumed after a crash: this week's scouting is already in the state (the paid part)
        self.state.reports = await scouting.scout_all(self.state.week)

    # ---------------------------------------------------------------- routing
    # and_: runs only once *both* scouting and the knowledge digest have finished.
    # Literal return type: the route labels, which flow.plot() / `crewai flow plot` use to draw the branches.
    @router(and_(scout_all, load_knowledge_digest))
    def assess_materiality(self) -> Literal["nothing_new", "routine", "material"]:
        previous = self.state.history[-1].threats if self.state.history else {}
        result = materiality.assess(self.state.reports, previous)
        self.state.materiality_score = result.score
        self.state.materiality_reasons = result.reasons
        route = result.route
        if route == "tie":
            route = self._tie_break(result)
        self.state.route = route
        return route  # type: ignore[return-value]

    def _tie_break(self, result: materiality.Assessment) -> Literal["routine", "material"]:
        """One Haiku call, only when the score sits exactly on the threshold."""
        signals = [s for r in self.state.reports.values() for s in r.signals if s.id in result.fresh_high]
        prompt = (
            "KiranaCloud (Kolkata kirana-store SaaS, Dukaan plan ₹799/month) runs a short routine brief, "
            "or a costly deep dive when a week is material. These are this week's new high-impact "
            "competitor signals:\n"
            + "\n".join(f"- [{s.id}] {s.detail}" for s in signals)
            + "\nIs this week material (warrants the deep dive)? Answer routine or material."
        )
        answer = make_llm().call([{"role": "user", "content": prompt}], response_model=TieBreak)
        tie = answer if isinstance(answer, TieBreak) else TieBreak.model_validate_json(str(answer))
        self.state.materiality_reasons.append(f"tie broken by LLM: {tie.route} ({tie.reason})")
        return tie.route

    # ---------------------------------------------------------------- the three branches
    @listen("nothing_new")
    def quiet_week(self) -> None:
        """No LLM: a quiet week gets a one-line brief."""
        previous = self.state.history[-1].threats if self.state.history else {}
        self.state.brief = Brief(
            week=self.state.week,
            headline="Quiet week: no new, credible competitor signals",
            tl_dr=["No competitor made a new, credible move this week."],
            since_last_week=["No change."],
            threats=[
                ThreatAssessment(
                    company=c.name,  # type: ignore[arg-type]  # a CompanyName Literal; story names match
                    threat=previous.get(c.name, "low"),  # type: ignore[arg-type]
                    rationale="Unchanged.",
                    signal_ids=[],
                )
                for c in COMPANIES
            ],
            recommendations=[],
            watchlist=[],
            route="routine",
        )
        # Nothing to review: a quiet week goes straight to publish.
        self.state.brief_markdown = render_markdown(self.state.brief)

    @listen("routine")
    def run_short_update(self) -> None:
        # Sync flow methods run in a worker thread (no event loop there), so the crew's plain kickoff() is
        # fine.
        from compintel.crews import short_update

        if self._brief_done():
            return
        self.state.brief = short_update.run(self.state.week, self.state.reports).brief

    @listen("material")
    def run_deep_dive(self) -> None:
        from compintel.crews import deep_dive

        if self._brief_done():
            return
        result = deep_dive.run(self.state.week, self.state.reports, variant=self.state.deep_dive_variant)
        self.state.brief = result.brief

    def _brief_done(self) -> bool:
        """Resumed after a crash: this week's brief is already in the state, so don't pay for the crew
        again."""
        return self.state.brief is not None and self.state.brief.week == self.state.week

    # ---------------------------------------------------------------- converge, review, publish
    @listen(or_(run_short_update, run_deep_dive))  # or_: whichever crew ran
    def render_draft(self) -> None:
        assert self.state.brief is not None
        self.state.brief_markdown = render_markdown(self.state.brief)
        # The crew's own version, to diff against what was published (lessons and revisions applied).
        draft = BRIEFS_DIR / "drafts" / f"week-{self.state.week:02d}.crew.md"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(self.state.brief_markdown)

    # or_: the first draft and every revision go to the same review: that's the revise loop.
    @listen(or_(render_draft, "revised"))
    @human_feedback(
        message="Approve this week's brief? Or say what to change, or reject it.",
        emit=["approve", "revise", "reject"],  # free-text feedback is mapped to one of these by `llm`
        llm=REVIEW_LLM,
        default_outcome="revise",  # silence isn't approval: empty feedback sends the brief back
        learn=True,  # distill lessons from feedback into memory; apply them to the next draft before review
        metadata={"stage": "editorial"},
    )
    def editorial_review(self) -> Brief:
        # Returns the Brief (not its markdown, as in the spec): with learn=True crewai pre-reviews a Pydantic
        # output as that same model (structured output), so the published .md and .json stay one document.
        assert self.state.brief is not None
        return self.state.brief

    # A router listening to a feedback outcome: revise, or stop looping once MAX_REVISIONS are spent.
    @router("revise")
    def revise_brief(self) -> Literal["revised", "revision_limit"]:
        reviewed = self._record_feedback()
        if self.state.revision_count >= MAX_REVISIONS:
            return "revision_limit"
        from compintel.crews import revision, short_update

        lessons = hitl_lessons(self.memory, reviewed.feedback or "editorial review")  # type: ignore[arg-type]
        self.state.brief = revision.run(
            review.as_brief(reviewed.output),  # what the reviewer saw (lessons already applied)
            reviewed.feedback,
            lessons,
            short_update.signals_input(self.state.reports),
        )
        self.state.revision_count += 1
        return "revised"

    @listen(or_(quiet_week, "approve", "revision_limit"))
    def publish(self) -> str:
        """briefs/week-NN.md + .json, and record this run's state id as the one next week forks from."""
        reviewed = self.last_human_feedback
        if reviewed is not None:
            if reviewed.outcome == "approve":
                self._record_feedback()
            # Publish what the human saw: with learn=True it can differ from what editorial_review returned.
            self.state.brief = review.as_brief(reviewed.output)
            self.state.brief_markdown = render_markdown(self.state.brief)
            if reviewed.outcome == "revise":  # the revision limit
                self.state.brief_markdown += (
                    f"\n> **Editor's note:** published after {MAX_REVISIONS} revisions; the reviewer's last "
                    f"request was not applied: {reviewed.feedback!r}\n"
                )
            self._write_feedback_audit()
        assert self.state.brief is not None
        BRIEFS_DIR.mkdir(exist_ok=True)
        md = BRIEFS_DIR / f"week-{self.state.week:02d}.md"
        md.write_text(self.state.brief_markdown)
        md.with_suffix(".json").write_text(self.state.brief.model_dump_json(indent=2))
        self.state.published_path = str(md)
        self._remember_signals()
        self._remember_brief(md)
        _update_index("published", self.state.week, self.state.id)
        _update_index("in_progress", self.state.week, None)
        return str(md)

    def _remember_signals(self) -> None:
        """This week's signals under /competitor/<company>, for next week's scouts and for Ask the Analyst.
        Written here, after the crews, not by the scouts (see memory.ReadOnlyScope). Every field is given, so
        no LLM call unless a fact repeats an earlier one (then one consolidation call: update or keep)."""
        importance = {"none": 0.2, "low": 0.4, "medium": 0.6, "high": 0.8}
        for company, report in self.state.reports.items():
            for s in report.signals:
                unverified = " (unverified rumor)" if s.source_credibility < 0.5 else ""
                competitor_scope(company).remember(
                    f"Week {self.state.week}: {s.detail}{unverified}",
                    categories=[s.kind],
                    importance=importance[s.impact_on_us],
                    metadata={"week": self.state.week, "signal_id": s.id, "source_ref": s.source_ref},
                    source="scout",
                )

    def _remember_brief(self, md: Path) -> None:
        """Flow memory: the published brief's facts under /brief/week-NN, tagged with where they came from so
        Ask the Analyst can cite the file. extract_memories is one LLM call; scope, categories and importance
        are given, so crewai skips its per-fact analysis call (it still consolidates near-duplicates)."""
        facts = self.extract_memories(self.state.brief_markdown)
        brief_scope(self.state.week).remember_many(
            facts,
            categories=["brief"],
            importance=0.7,
            metadata={"week": self.state.week, "file": str(md)},
            source="brief",
        )

    @listen("reject")
    def archive(self) -> str:
        """Rejected: kept for the record, not published. Next week can't fork it; rerun this week."""
        reviewed = self._record_feedback()
        path = BRIEFS_DIR / "rejected" / f"week-{self.state.week:02d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown(review.as_brief(reviewed.output)))
        self._write_feedback_audit()
        _update_index("in_progress", self.state.week, None)
        return str(path)

    def _record_feedback(self) -> HumanFeedbackResult:
        """Append the latest review to the state's audit log. Not crewai's human_feedback_history: that lives
        on the Flow object, not in the persisted state, so a review resumed in a new process loses it."""
        reviewed = self.last_human_feedback
        assert reviewed is not None and self.state.brief is not None
        self.state.feedback_log.append(
            {
                "round": len(self.state.feedback_log) + 1,
                "outcome": reviewed.outcome,
                "feedback": reviewed.feedback,
                "at": reviewed.timestamp.isoformat(),
                "metadata": reviewed.metadata,
                "headline_reviewed": review.as_brief(reviewed.output).headline,
                # True when the learn=True pre-review rewrote the draft before the human saw it.
                "changed_by_lessons": review.as_brief(reviewed.output) != self.state.brief,
            }
        )
        return reviewed

    def _write_feedback_audit(self) -> None:
        BRIEFS_DIR.mkdir(exist_ok=True)
        (BRIEFS_DIR / f"week-{self.state.week:02d}.feedback.json").write_text(
            json.dumps(
                {"week": self.state.week, "state_id": self.state.id, "reviews": self.state.feedback_log},
                indent=2,
                ensure_ascii=False,
            )
        )


def run_week(
    week: int,
    deep_dive_variant: str = "manager_agent",
    resume: bool = False,
    reviewer: Any = None,
) -> Flow[CIState]:
    """Run one week. With resume=True, fork the state of this week's unfinished run: completed paid steps
    (scouting, the crew) are skipped because their results are already in the state (@persist saved it after
    every method). A fork, not crewai's deprecated inputs.id resume, so the crashed run stays on record.

    `reviewer`: the HumanFeedbackProvider for the review step (None: the one already installed, else the
    console). With review.PendingReview the run pauses at the review; see `is_paused`."""
    flow = CompetitiveIntelFlow()
    inputs = {"week": week, "deep_dive_variant": deep_dive_variant}
    previous = review.use(reviewer) if reviewer is not None else None
    try:
        if resume:
            crashed = read_state_index()["in_progress"].get(str(week))
            if crashed is None:
                raise ValueError(f"no unfinished run of week {week} to resume")
            flow.kickoff(inputs=inputs, restore_from_state_id=crashed)
        else:
            flow.kickoff(inputs=inputs)  # returns a HumanFeedbackPending if the review paused it
    finally:
        if reviewer is not None:
            review.use(previous)
    return flow


def is_paused(flow: Flow[CIState]) -> bool:
    """Waiting for an async review (kickoff returned HumanFeedbackPending)."""
    return (review.pending_dir() / f"{flow.state.id}.json").exists()


def kickoff() -> None:
    """`crewai run` without --inputs: the week comes from $WEEK (default 1)."""
    os.chdir(PROJECT_ROOT)  # briefs/ and the crews' output paths are cwd-relative
    run_week(int(os.environ.get("WEEK", "1")))


def plot() -> str:
    """`crewai flow plot`: docs/flow_plot.html (+ its .css/.js), with every router branch labelled."""
    html = CompetitiveIntelFlow().plot("flow_plot.html", show=False)
    src, docs = Path(html).parent, PROJECT_ROOT / "docs"
    for f in src.iterdir():  # crewai renders into a temp dir: the html plus *_style.css and *_script.js
        shutil.copy(f, docs / f.name)
    print(f"wrote {docs / 'flow_plot.html'}")
    return str(docs / "flow_plot.html")
