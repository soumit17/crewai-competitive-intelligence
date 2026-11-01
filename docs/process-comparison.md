# Process comparison: sequential vs `manager_llm` vs custom `manager_agent`

Spec §8.3 calls this the most important CrewAI lesson. Same DeepDiveCrew agents, tasks, guardrails, knowledge
and crew planning; same week-2 scout reports; Haiku 5.5 everywhere; one live run each (2026-11-01).
Regenerate with `scripts/process_comparison.py` (see its docstring). Raw data: `runs/process_comparison.json`.

<!-- table:begin -->
| | `sequential` | `manager_llm` | `manager_agent` |
|---|---|---|---|
| Wall time | 339 s | 155 s | 186 s |
| LLM calls | 69 | 16 | 20 |
| Input / output tokens | 310,394 / 70,556 | 115,714 / 26,845 | 149,384 / 36,446 |
| Cost (Haiku 5.5) | $0.0668 | $0.0257 | $0.0331 |
| of which crew planning | $0.0069 | $0.0040 | $0.0072 |
| Manager calls | 0 | 8 | 10 |
| Specialist calls | 59 | 0 | 0 |
| Delegations (to intended agent) | 0 (0) | 0 (0) | 0 (0) |
| Guardrail failures | 2 | 0 | 1 |
| Escalation memo ran | yes | yes | yes |
| Material signals cited | 4/4 | 4/4 | 4/4 |
| Threat levels vs ground truth | 3/3 | 3/3 | 2/3 |
| Kolkata threat leads the TL;DR | yes | yes | yes |
| Error | - | - | - |
<!-- table:end -->

## What happened

**Neither manager delegated anything.** Both hierarchical variants were offered crewai's
"Delegate work to coworker" and "Ask question to coworker" tools on every call (pinned by
`test_manager_delegates_every_task_through_crewais_tool`), and Haiku answered all six tasks itself every time,
across three live hierarchical runs (two with the custom Head of Strategy, one with crewai's generic manager).
No specialist ever ran, so their tools (`pricing_parse`, `changelog_diff`, `news_search`), their agent-level
knowledge and their `PlanningConfig` never came into play. "Hierarchical" became "one generalist with all the
context", which is why it is cheaper and faster here, not slower as manager overhead would suggest.

**Sequential paid for specialists, mostly through agent planning.** 59 of its 69 calls were specialists, and the
Market Analyst alone made 34 (its `PlanningConfig` loop plus a `threat_levels_consistent` retry); the Strategist
made 19 (`PlanningConfig(reasoning_effort="medium", max_attempts=2)`). The three analyses did run in parallel, but
the planning loops cost more time than parallelism saved.

**Quality was close, and sequential was the most reliable.** All three cited the 4 material signals, ran the
escalation memo (the synthesis rated a competitor high) and led with the Kolkata threat. Sequential and
`manager_llm` matched all 3 expected threat levels; the custom manager rated ShelfMitra medium. Sequential's two
guardrail failures were both caught and fixed on retry, which is the guardrails doing their job.

**crewai constraints the hierarchical process imposed** (docs/decisions.md):
- every task runs on the manager's single executor, so `async_execution` had to be switched off (concurrent
  use raised "Executor is already running", and with knowledge on it ended the process silently);
- tasks keep no agent (otherwise the manager may delegate only to that agent), so a plain-string guardrail can't
  bind to a model and the rule had to be an explicit `LLMGuardrail`;
- tool-only steps (M3's `result_as_answer` render) can't be relied on, so rendering moved to `@after_kickoff`.

## Was the manager worth it?

Not as a manager. In these runs it never managed: the custom role, goal and backstory ("delegate each piece to
the best-placed specialist") did not change that. Its value showed up only as a cheaper single-agent run.
If the goal is specialist work (tools, scoped knowledge, planning), the explicit `sequential` process
delivers it predictably; if the goal is a cheap, good-enough deep dive, one capable agent with the signals in
its prompt did as well here at about 40% of the cost.

Worth trying before drawing a general conclusion (not run, to keep M4 in budget): the same comparison with
the manager on Sonnet (`--final`-style), and `sequential` without agent planning to separate the cost of the
process from the cost of `PlanningConfig`.
