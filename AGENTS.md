# AGENTS.md — guide for coding agents working in this repo

- The spec is [p3-crewai-competitive-intelligence.md](p3-crewai-competitive-intelligence.md). Its §0 rules apply:
  one milestone at a time, verify CrewAI APIs against the installed source before coding, Anthropic-only,
  native features over hand-rolled ones, $0 tests.
- Verified versions, API names and every OpenAI default: [docs/decisions.md](docs/decisions.md).
- Never set or require `OPENAI_API_KEY`. Every LLM / embedder / planner / judge is configured explicitly from
  `src/compintel/config.py`.
- Tests: `uv run pytest` — network blocked except `127.0.0.1`, no model calls (ScriptedLLM).
- Live calls only from `scripts/` and `evals/`, and every run is written to the cost ledger.
- Never commit `.env`.

## Where things stand (handoff)

- **Done: M0–M9** (baseline in `evals/baseline.json`, `python -m evals run --gate` before merging changes to prompts or crews), one commit each (`git log`). The README's status line and its per-milestone sections say what
  each milestone added and how to run it; `docs/decisions.md` has every decision and live finding, numbered.
- **Next:** the developer's journal (`notes/p3-journal.md`); stretch goals (spec §22) if wanted.
  Live reviews: use `--review async` (or `P3_REVIEW=async` for `crewai run`) + `compintel review <id> "approve"`; the console prompt received only empty
  answers on the developer's terminal (decisions #106, unexplained; not reproducible here).
  M8 was split into stages at the developer's request: each ends with commands for the developer to run.
- **Spend**: `uv run compintel costs` (about $2.13 of the $12 cap after M8). The repo moved machines after M7:
  `runs/costs.jsonl` starts with one `carried-forward` row of $1.34 for the earlier spend.
- **Working agreement with the developer**:
  - Start each milestone with the API verification pass (installed source + docs), then implement, test, run
    live, document (README section + decisions + findings), commit locally, and stop with a summary.
  - The developer pushes to GitHub; commit locally only.
  - Ask before hand-rolling anything crewai provides.
- **Live runs need the fake web server**: `uv run python scripts/serve_fakeweb.py` (stop it afterwards). Weeks
  run in order (week N forks week N-1's published state, `runs/state_index.json`).
  - `compintel run --week N --review async` pauses at the review step; continue with `compintel pending` /
    `compintel review <id> "feedback"`.
  - Week 2 (material): use `--variant sequential`, since the manager variant failed its guardrails live in M5.
  - Run live commands with `PYTHONUNBUFFERED=1` and check exit codes.
- **Gotchas found so far (details in decisions.md)**:
  - Agents' memory views must recall shallow and must not save mid-run (`memory.py`), or crew checkpoints break.
  - Scouts use BaseTool twins, not `@tool`.
  - A deep dive can leave `sys.stdout` on a StringIO (`runtime.restore_std_streams`).
  - `@on` methods on a `@CrewBase` class register globally and are never removed: guard by crew identity.
  - An `INPUT` hook must not add keys when a crew resumes from a checkpoint (`hooks.inputs_defaults`).
  - Tracing: Phoenix's server runs under `uvx` (not in the venv). Adding a span processor to Phoenix's
    TracerProvider needs `replace_default_processor=False`, or nothing is exported.
  - `crewai_event_bus.scoped_handlers()` hides *every* handler (the cost listener too) while it lasts: tests only.
  - Never train into `trained_agents_data.pkl` (every agent run reads it by default, by role).
  - A checkpointing crew can't be kicked off twice: the second kickoff resumes and skips every task
    (`crewai test`/`train` run with `checkpoint = False`, decisions #125). Training keeps one feedback per agent
    per iteration (#126).
  - An agent `guardrail`'s retry crashes under `Agent.kickoff_async` (crewai calls the sync executor inside
    the loop): the scouts run `kickoff` in worker threads (#132).
  - A fake web server left running holds port 8765: `pgrep -af serve_fakeweb` before starting another.
- **Tests**: 252 passing (`uv run pytest`, about 82 s; the spec's target is 60 s). `uv run ruff check .`,
  `uv run pyright` are clean.
