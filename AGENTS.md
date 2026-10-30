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
