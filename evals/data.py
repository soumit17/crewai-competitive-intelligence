"""What the evals score: each published week, as the Flow persisted it (the reports it ran on, the brief, the
route). `runs/state_index.json` says which state is published for each week."""

from __future__ import annotations

from dataclasses import dataclass

from compintel.models import Brief, CIState, SignalReport
from compintel.world.story import GROUND_TRUTH


@dataclass
class WeekRun:
    week: int
    state_id: str
    reports: dict[str, SignalReport]
    brief: Brief
    route: str

    @property
    def truth(self) -> dict:
        return GROUND_TRUTH[self.week]

    @property
    def signals(self) -> dict:
        """Every signal the scouts reported this week, by id."""
        return {s.id: s for r in self.reports.values() for s in r.signals}


def load_weeks(weeks: tuple[int, ...] = (1, 2, 3)) -> list[WeekRun]:
    """The published runs of `weeks`. Raises if one isn't published: score all three or say which."""
    from compintel.runtime import bootstrap

    bootstrap()  # env before crewai's import; the cost listener and budget gate for the judges
    from crewai.flow.persistence import SQLiteFlowPersistence

    from compintel.flow import read_state_index

    published = read_state_index()["published"]
    persistence = SQLiteFlowPersistence()
    runs = []
    for week in weeks:
        state_id = published.get(str(week))
        data = persistence.load_state(state_id) if state_id else None
        if not state_id or not data:
            raise ValueError(f"week {week} isn't published; run it first (compintel run --week {week})")
        state = CIState.model_validate(data)
        if state.brief is None:
            raise ValueError(f"week {week}'s published state {state_id} has no brief")
        runs.append(WeekRun(week, state_id, state.reports, state.brief, state.route))
    return runs
