# P3 learning journal (CrewAI)

The spec's §23 questions, for the developer to answer. Under each: where this repo's evidence is (decision
numbers are `docs/decisions.md`). The answers are yours; nothing below is one.

## 1. Crews vs Flows: where did you draw the line, and why?
Evidence: README "How one weekly run flows"; decisions #54-#56 (the router is code).

## 2. Sequential vs hierarchical (manager_llm vs custom manager): cost, quality, delegation — was the manager worth it?
Evidence: docs/process-comparison.md; README M4; M5 live week 2 (`manager_agent` failed its guardrails).

## 3. What did role/goal/backstory actually change? Backstory length vs tokens vs quality.
Evidence: README M3 "Backstory length"; `scripts/backstory_experiment.py`.

## 4. Which OpenAI defaults bit you, and how did you find them?
Evidence: decisions table at the top of docs/decisions.md; `tests/test_no_openai.py`.

## 5. `@persist` fork vs resume vs crew checkpointing — when each?
Evidence: README M5 "Checkpointing and replay"; decisions #59, #125; docs/what-if.md.

## 6. `@human_feedback` (`emit`, `learn`) vs `human_input=True` vs hook approval — which would you ship?
Evidence: README M6 "Three ways to put a human in the loop"; decisions #106 (console prompt), M6 findings.

## 7. Unified memory: scopes vs slices; did LLM-inferred scopes help or confuse? Memory vs knowledge?
Evidence: README M7; `scripts/memory_exercises.py`; M7 findings.

## 8. Function vs string guardrails — reliability and cost.
Evidence: README M3 "Guardrails"; `GuardrailLog` outcomes in docs/what-if.md; M5 week 2 failure.

## 9. Hooks vs event listeners — control vs observation.
Evidence: README M8a "Hooks control, listeners watch".

## 10. Did `PlanningConfig` improve the Strategist? Cost of planning?
Evidence: README M4; decisions #107 (planner truncation), M8b findings.

## 11. Cost per weekly run (routine vs material) and where tokens went.
Evidence: README "Results"; docs/observability.md (token share by agent); `uv run compintel costs`.

## 12. Compared with P1 (Pydantic AI) and P2 (Workflows): what does CrewAI make easy, and what does it hide from you?
Evidence: the crewai bugs and hidden defaults in decisions (#59, #125, #126, the OpenAI table); your P1/P2 notes.
