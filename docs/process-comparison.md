# Process comparison: sequential vs `manager_llm` vs custom `manager_agent`

Spec §8.3 calls this the most important CrewAI lesson. Same DeepDiveCrew agents, tasks, guardrails, knowledge
and crew planning; same week-2 scout reports; Haiku 5.5 everywhere; one live run each (2026-11-01).
Regenerate with `scripts/process_comparison.py` (see its docstring). Raw data, including every delegation and
every tool call by agent: `runs/process_comparison.json`.

<!-- table:begin -->
| | `sequential` | `manager_llm` | `manager_agent` |
|---|---|---|---|
| Wall time | 207 s | 217 s | 304 s |
| LLM calls | 35 | 22 | 28 |
| Input / output tokens | 169,851 / 46,738 | 135,960 / 39,890 | 170,925 / 57,121 |
| Cost (Haiku 5.5) | $0.0413 | $0.0335 | $0.0425 |
| of which crew planning | $0.0057 | $0.0054 | $0.0036 |
| Manager calls | 0 | 8 | 9 |
| Specialist calls | 25 | 4 | 6 |
| Delegations (to intended agent) | 0 (0) | 2 (2) | 3 (2) |
| Guardrail failures | 1 | 0 | 1 |
| Recovered LLM call failures | 0 | 0 | 0 |
| Failed planning observations | 0 | 0 | 0 |
| Escalation memo ran | yes | yes | yes |
| Material signals cited | 4/4 | 4/4 | 4/4 |
| Threat levels vs ground truth | 3/3 | 3/3 | 3/3 |
| Kolkata threat leads the TL;DR | yes | yes | yes |
| Error | - | - | - |
<!-- table:end -->

## What happened

**The managers delegated the tool work and kept the thinking.** Both managers handed `pricing_analysis` and
`product_overlap` to the intended specialists, whose instructions name a tool (`pricing_parse`,
`changelog_diff`). They did `market_threats`, `synthesis`, the escalation memo and the brief themselves. The Head of
Strategy also asked the Market Analyst for a second view on pricing: its one delegation to a non-intended agent.
So the Market Analyst and Strategist, with their `PlanningConfig`s, barely ran in the hierarchical variants
(4-6 specialist calls, against 25 in `sequential`).

**Cost was a wash; time was not.** All three cost $0.033-0.042. `sequential` was fastest (207 s): its three analyses
run in parallel. The hierarchical variants run every task one after another on the manager (crewai constraint,
below), and the custom manager's review-and-delegate turns made it the slowest (304 s).

**Quality was identical** on every deterministic check: 4/4 material signals cited, 3/3 threat levels, Kolkata
first, escalation memo run, no rumor claimed. No recovered LLM failures, no failed planning observations.

**crewai constraints the hierarchical process imposed** (docs/decisions.md):
- every task runs on the manager's single executor, so `async_execution` had to be switched off (concurrent use
  raised "Executor is already running"; with knowledge on, the same collision ended the process silently);
- tasks keep no agent and no tools, or the manager may delegate only to that agent, or does the work itself with
  the specialist's tools (a Task copies its agent's tools into `task.tools`);
- a plain-string guardrail needs a task agent, so the rule is an explicit `LLMGuardrail`;
- tool-only steps (M3's `result_as_answer` render) can't be relied on, so rendering moved to `@after_kickoff`.

## Was the manager worth it?

Not for this crew. The routing here is known in advance (one specialist per task), and the manager matched
`sequential`'s quality at the same cost while giving up parallelism and making the run less predictable: who did
which task varied by manager, and most tasks were done by the manager rather than the specialist designed for
them. A manager earns its place when the routing itself is the hard part (many interchangeable specialists, or
work whose shape isn't known up front). For a fixed analysis pipeline, the explicit `sequential` process is the
better default, and the cheapest way to get specialist behaviour (tools, scoped knowledge, planning) reliably.

## Two earlier versions of this table were wrong

1. **Errors hidden.** The first comparison ran with stderr sent to /dev/null. Every Strategist planning
   observation and some `ThreatAssessments` replies had failed in crewai's tool-based structured-output fallback
   for Haiku 5.5 (fixed: `compat.py`), and the failures had shortened the planning loop, so `sequential` looked
   like 69 calls and $0.067. Once observations worked, unbounded replanning ran away (144 Strategist calls before a
   timeout), fixed by bounding `max_replans` / `max_step_iterations`.
2. **Managers that couldn't fail to look lazy.** The hierarchical variants cleared each task's agent but not its
   tools, so the manager was handed the specialists' tools and did all the work itself: "0 delegations" in three
   runs, reported as "Haiku managers don't delegate". With only the delegation tools, they delegate as above.

Both are now pinned by tests, and the comparison records recovered failures and every tool call so neither can
hide again.
