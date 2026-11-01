"""CompetitiveIntelFlow (spec §9): one week, end to end. The Flow owns state and routing; crews do the fuzzy
parts.

    load_week ─┐                      (+ load_knowledge_digest, a parallel @start)
               └ scout_all ─ and_ ─ assess_materiality (@router)
                                      ├ "nothing_new" → quiet_week ─────┐
                                      ├ "routine"     → run_short_update ┼ render_draft → publish
                                      └ "material"    → run_deep_dive ──┘

M6 puts human review between render_draft and publish; M7 adds memory.

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
from crewai.flow.persistence import persist

from compintel import materiality
from compintel.config import PROJECT_ROOT, get_settings, make_llm
from compintel.models import Brief, CIState, ThreatAssessment, TieBreak, WeekSummary
from compintel.runtime import bootstrap
from compintel.tools.render_brief import render_markdown
from compintel.world.story import COMPANIES, WEEKS

BRIEFS_DIR = Path("briefs")  # cwd-relative, like the crews' drafts


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
    def kickoff(self, inputs: dict[str, Any] | None = None, **kwargs: Any) -> Any:  # type: ignore[override]
        """Bootstrap (hooks and cost listener; scouts and crews refuse to run without them), and for weeks
        after
        the first, fork last week's published state unless a `restore_from_state_id` is given."""
        bootstrap()
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

    # ---------------------------------------------------------------- converge and publish
    @listen(or_(quiet_week, run_short_update, run_deep_dive))  # or_: whichever branch ran
    def render_draft(self) -> None:
        assert self.state.brief is not None
        self.state.brief_markdown = render_markdown(self.state.brief)

    @listen(render_draft)
    def publish(self) -> str:
        """briefs/week-NN.md + .json, and record this run's state id as the one next week forks from."""
        assert self.state.brief is not None
        BRIEFS_DIR.mkdir(exist_ok=True)
        md = BRIEFS_DIR / f"week-{self.state.week:02d}.md"
        md.write_text(self.state.brief_markdown)
        md.with_suffix(".json").write_text(self.state.brief.model_dump_json(indent=2))
        self.state.published_path = str(md)
        _update_index("published", self.state.week, self.state.id)
        _update_index("in_progress", self.state.week, None)
        return str(md)


def run_week(week: int, deep_dive_variant: str = "manager_agent", resume: bool = False) -> Flow[CIState]:
    """Run one week. With resume=True, fork the state of this week's unfinished run: completed paid steps
    (scouting, the crew) are skipped because their results are already in the state (@persist saved it after
    every method). A fork, not crewai's deprecated inputs.id resume, so the crashed run stays on record."""
    flow = CompetitiveIntelFlow()
    inputs = {"week": week, "deep_dive_variant": deep_dive_variant}
    if resume:
        crashed = read_state_index()["in_progress"].get(str(week))
        if crashed is None:
            raise ValueError(f"no unfinished run of week {week} to resume")
        flow.kickoff(inputs=inputs, restore_from_state_id=crashed)
    else:
        flow.kickoff(inputs=inputs)
    return flow


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
