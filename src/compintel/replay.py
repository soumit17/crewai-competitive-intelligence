"""`crewai replay -t <task_id>` (spec §13): re-run the latest crew kickoff from one task, reusing the stored
outputs
of the tasks before it. crewai's CLI runs this project's `replay` script with the task id (`crewai
log-tasks-outputs` lists the ids). Which crew ran last is read from the stored outputs themselves.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from crewai import Crew, CrewOutput


def latest_crew(fake: Any = None) -> Crew:
    """Rebuild the crew whose kickoff the stored task outputs came from (3 tasks: short update; 6: deep
    dive)."""
    from crewai.memory.storage.kickoff_task_outputs_storage import KickoffTaskOutputsSQLiteStorage

    from compintel.crews import short_update
    from compintel.crews.deep_dive import DeepDiveCrew

    stored = KickoffTaskOutputsSQLiteStorage().load()
    if not stored:
        raise ValueError("no stored crew kickoff to replay; run a crew first")
    if len(stored) == 3:
        return short_update.load(fake=fake)
    agents = {row["output"].get("agent") for row in stored}
    variant = (
        "manager_agent"
        if "Head of Strategy" in agents
        else "manager_llm"
        if "Crew Manager" in agents
        else "sequential"
    )
    return DeepDiveCrew(variant=variant, fake=fake).crew()  # type: ignore[call-arg]


def replay(task_id: str, fake: Any = None) -> CrewOutput:
    from compintel.guardrails import register_signals

    crew = latest_crew(fake)
    # Crew.replay doesn't run before-kickoff callbacks; the guardrails need this run's signals registered.
    from crewai.memory.storage.kickoff_task_outputs_storage import KickoffTaskOutputsSQLiteStorage

    register_signals(KickoffTaskOutputsSQLiteStorage().load()[0]["inputs"] or {})
    return crew.replay(task_id=task_id)


def main() -> None:
    """Console script `replay` (what `crewai replay -t <id>` runs)."""
    from compintel.config import PROJECT_ROOT
    from compintel.runtime import bootstrap

    bootstrap()
    os.chdir(PROJECT_ROOT)
    result = replay(sys.argv[1])
    print(result.raw)
