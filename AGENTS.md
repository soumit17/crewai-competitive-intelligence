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

- **Done: M0–M7**, one commit each (`git log`). The README's status line and its per-milestone sections say what
  each milestone added and how to run it; `docs/decisions.md` has every decision and live finding, numbered.
- **Next: M8** (spec §20: hooks, observability/tracing, evals, `crewai test` / `crewai train`), then M9.
- **Spend**: `uv run compintel costs` (about $1.34 of the $12 cap after M7).
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
- **Tests**: 162 passing (`uv run pytest`, about 65 s; the spec's target is 60 s). `uv run ruff check .`,
  `uv run pyright` are clean.
