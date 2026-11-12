"""`crewai test` and `crewai train` on the ShortUpdateCrew (spec §17.2, M8d).

crewai's CLI doesn't run a crew itself: `crewai test -n N -m MODEL` runs `uv run test N MODEL` and
`crewai train -n N -f FILE` runs `uv run train N FILE`, i.e. this project's `test` / `train` scripts
(pyproject.toml), which call `Crew.test` / `Crew.train`. The crew runs on a published week's signals
(`WEEK`, default 3).

- test: `Crew.test(n, eval_llm)` runs the crew n times; after each task an evaluator agent on `eval_llm`
  scores it 1-10. The CLI's help says "only OpenAI models" and defaults to gpt-5.4-mini, but `Crew.test` takes
  any LLM: the model string is built here through config (native Anthropic provider, our structured-output
  registration), so `-m anthropic/claude-haiku-5-5` works.
- train: `Crew.train(n, file)` sets `human_input=True` on every task; after each task the agent's answer
  gets human feedback and is redone with it; then each agent's own LLM (here Haiku) turns the feedback into
  suggestions, saved by agent role in FILE. Later runs append those suggestions to every task prompt of an
  agent with that role (`Agent._use_trained_data`).

Two choices:
- Feedback comes from `evals/training_feedback.json` (task name -> feedback, reviewed before training),
  through crewai's own provider hook (`crewai.core.providers.human_input.set_provider`), not a console
  prompt: same training path, no typing. crewai keeps one feedback record per agent per iteration, so of
  an agent's tasks only one may get feedback (the writer's write_short_brief; render_draft gets none).
- Checkpointing is off for both: see `_crew`.
- The file must not be crewai's default `trained_agents_data.pkl`: with no file named, *every* agent run reads
  that one from the working directory, by role, so the deep dive's Brief Writer and Market Analyst would
  silently follow the ShortUpdateCrew's training. Train into runs/training/ and load it only on purpose
  (`short_update.run(..., trained_agents_file=...)`, scripts/compare_trained.py).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from crewai.core.providers.human_input import SyncHumanInputProvider

from compintel.config import PROJECT_ROOT, get_settings

FEEDBACK_FILE = PROJECT_ROOT / "evals" / "training_feedback.json"
DEFAULT_CREWAI_FILE = "trained_agents_data.pkl"


class FileFeedback(SyncHumanInputProvider):
    """crewai's console provider, with the answer taken from a file by task name instead of `input()`.

    Training only: crewai's training path (`_handle_training_feedback`) records the agent's answer and the
    feedback, then has the agent redo the task with the feedback, exactly as after a typed answer."""

    def __init__(self, feedback: dict[str, str]) -> None:
        self.feedback = feedback
        self.used: list[tuple[str, str]] = []

    def handle_feedback(self, formatted_answer: Any, context: Any) -> Any:
        task = getattr(context, "task", None)
        name = getattr(task, "name", None) or ""
        text = self.feedback.get(name, "")
        self.used.append((name, text))
        print(f"\n[training feedback for {name or 'unnamed task'}] {text or '(none)'}\n", flush=True)
        if text and context._is_training_mode():
            return self._handle_training_feedback(formatted_answer, text, context)
        # No feedback, no round: crewai keeps one training record per agent per iteration, so a round here
        # would overwrite the record of this agent's earlier task (the writer's write_short_brief).
        return formatted_answer


def load_feedback(path: Path = FEEDBACK_FILE) -> dict[str, str]:
    data = json.loads(path.read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


def week_inputs(week: int | None = None) -> tuple[int, dict]:
    """The week the crew trains / tests on and its published scout reports."""
    from crewai.flow.persistence import SQLiteFlowPersistence

    from compintel.flow import read_state_index
    from compintel.models import CIState

    week = week or int(os.environ.get("WEEK", "3"))
    state_id = read_state_index()["published"].get(str(week))
    data = SQLiteFlowPersistence().load_state(state_id) if state_id else None
    if not data:
        raise SystemExit(f"week {week} isn't published; run it first (compintel run --week {week})")
    return week, CIState.model_validate(data).reports


def _crew(fake: Any = None) -> Any:
    """The ShortUpdateCrew with checkpointing off.

    `Crew.test` / `Crew.train` kick off one crew object n times. A checkpoint write copies the run's kickoff
    id onto the live crew (`crewai.state.runtime._sync_checkpoint_fields`), so the second kickoff takes itself
    for a checkpoint resume and skips every finished task: n=2 ran the crew once (decisions #125)."""
    from compintel import hooks
    from compintel.crews import short_update

    hooks.install()
    crew = short_update.load(fake=fake)
    crew.checkpoint = False  # crewai: False opts out, None inherits
    return crew


def run_test(n_iterations: int, model: str, fake: Any = None, eval_llm: Any = None) -> None:
    from compintel.config import make_llm
    from compintel.crews import short_update

    week, reports = week_inputs()
    llm = eval_llm or make_llm(model=model)
    print(f"crewai test: ShortUpdateCrew on week {week}, {n_iterations} runs, evaluator {llm.model}")
    _crew(fake).test(n_iterations, eval_llm=llm, inputs=short_update.inputs_for(week, reports))


def run_train(
    n_iterations: int, filename: str, fake: Any = None, feedback: dict[str, str] | None = None
) -> None:
    from crewai.core.providers.human_input import reset_provider, set_provider

    from compintel.crews import short_update

    if Path(filename).name == DEFAULT_CREWAI_FILE:
        raise SystemExit(
            f"refusing {DEFAULT_CREWAI_FILE}: every agent run reads that file by default (see "
            "compintel.training). Use crewai train -f runs/training/short_update.pkl"
        )
    Path(filename).parent.mkdir(parents=True, exist_ok=True)
    week, reports = week_inputs()
    provider = FileFeedback(feedback if feedback is not None else load_feedback())
    token = set_provider(provider)
    try:
        print(f"crewai train: ShortUpdateCrew on week {week}, {n_iterations} iterations -> {filename}")
        _crew(fake).train(n_iterations, filename, inputs=short_update.inputs_for(week, reports))
    finally:
        reset_provider(token)
    print(f"feedback given {len(provider.used)} times; trained file: {filename}")


def _bootstrap_here() -> None:
    from compintel.runtime import bootstrap

    os.chdir(PROJECT_ROOT)  # crewai's training_data.pkl and the crew's output paths are cwd-relative
    bootstrap()
    if not get_settings().anthropic_api_key:
        raise SystemExit("ANTHROPIC_API_KEY is not set; put it in .env")


def test() -> None:
    """`uv run test N MODEL` (what `crewai test -n N -m MODEL` runs)."""
    _bootstrap_here()
    run_test(int(sys.argv[1]), sys.argv[2])


def train() -> None:
    """`uv run train N FILE` (what `crewai train -n N -f FILE` runs)."""
    _bootstrap_here()
    run_train(int(sys.argv[1]), sys.argv[2])
