# CompIntel (P3): CrewAI Crews + Flows

A weekly competitive-intelligence brief for **KiranaCloud** (fictional Kolkata B2B SaaS) about three fictional
competitors, built on CrewAI 1.15.33 with Anthropic Claude only.
Full spec: [p3-crewai-competitive-intelligence.md](p3-crewai-competitive-intelligence.md).
Version pins, API verification and every deviation from the spec: [docs/decisions.md](docs/decisions.md).

**Status:** M0 (Anthropic-only setup, $0 test fakes, cost ledger + budget gate, live hello) done.

## Setup

```bash
uv sync                      # creates the project's own .venv (Python 3.12) from uv.lock; crewai needs <3.14
cp .env.example .env         # then put your ANTHROPIC_API_KEY in .env (no OpenAI key, ever)
uv run pytest                # $0: network blocked, no model calls, OPENAI_API_KEY unset
```

`uv run …` always uses the project's `.venv`, never the system Python (3.14, which crewai doesn't support).

## M0: run it yourself

| Command | Cost | What to look at |
|---|---|---|
| `uv run pytest` | $0 | 16 tests, ~2 s. `tests/fakes.py` is the fake model everything is tested with |
| `uv run compintel hello` | ~$0.0005 | One live Haiku call: a single agent classifies a headline into a typed `HeadlineTake` |
| `uv run compintel hello "BahiBook hires a PM for GST e-invoicing"` | ~$0.0005 | Your own headline: watch `impact_on_us` change |
| `uv run compintel hello --verbose` | ~$0.0005 | crewai's own trace of the agent run |
| `uv run compintel hello --final` | ~$0.01 | Same call on Sonnet 5.5 (`MODEL_FINAL`) |
| `uv run compintel costs` | $0 | Spend per model and agent from `runs/costs.jsonl`, against the $12 cap |

### What happens when you run `compintel hello`

1. `apply_env()` exports `MODEL=anthropic/claude-haiku-5-5` and turns crewai telemetry/tracing off,
   **before** crewai is imported (crewai reads these on import).
2. `listeners.install()` registers `CostListener` on crewai's **event bus**, and `hooks.install()` registers
   `budget_gate` as an **`@on(PRE_MODEL_CALL)` hook**.
3. `Agent(...).kickoff(prompt, response_format=HeadlineTake)` runs **one agent with no crew** (a "LiteAgent" run).
4. Before the API request, `budget_gate` runs. If the ledger is at the $12 cap it raises `HookAborted`, and the call never happens.
5. The native Anthropic provider sends the request with the `HeadlineTake` JSON schema and parses the reply
   into the Pydantic object (`out.pydantic`).
6. The provider emits `LLMCallCompletedEvent` with token usage. `CostListener` prices it and appends a row to
   `runs/costs.jsonl` (on a background thread, so the CLI calls `crewai_event_bus.flush()` before printing).

### Where each M0 concept lives

| Concept | Code |
|---|---|
| Native Anthropic provider, explicit LLM / limits / embedder (no OpenAI defaults) | [src/compintel/config.py](src/compintel/config.py) |
| `Agent.kickoff` + `response_format` (agent without a crew, typed output) | [src/compintel/hello.py](src/compintel/hello.py) |
| Event bus + `BaseEventListener` → cost ledger | [src/compintel/listeners.py](src/compintel/listeners.py), [src/compintel/cost.py](src/compintel/cost.py) |
| `@on` hook + `HookAborted` → spend cap | [src/compintel/hooks.py](src/compintel/hooks.py) |
| Custom LLM interface (`BaseLLM`) → `ScriptedLLM`; `FakeEmbedder` | [tests/fakes.py](tests/fakes.py) |
| $0 guards: no network, no OpenAI key, hooks/handlers cleared per test | [tests/conftest.py](tests/conftest.py), [tests/test_no_openai.py](tests/test_no_openai.py) |

## Later milestones' commands

| Command | Cost | Milestone |
|---|---|---|
| `python scripts/gen_world.py` · `python scripts/serve_fakeweb.py` | $0 | M1 |
| `uv run compintel run --week 1\|2\|3` | ~$0.3–0.6 / week | M5 |
| `uv run compintel chat` | cents | M7 |

## How one weekly run flows, end to end

The one idea to hold onto: **a Flow is the control layer** (plain Python decides *when* and *which*), and
**Crews/Agents are the autonomy layer** (LLMs decide *how*). The Flow owns state and routing and calls a crew
only for the fuzzy parts.

```mermaid
flowchart TD
    S([compintel run --week N]) --> L["load_week<br/>recall last week's memory"]
    L --> SA["scout_all<br/>3 Scout agents in parallel"]
    SA --> R{"assess_materiality<br/>(router: mostly code)"}
    R -->|nothing_new| Q[quiet_week]
    R -->|routine| SU[[ShortUpdateCrew<br/>sequential]]
    R -->|material| DD[[DeepDiveCrew<br/>hierarchical, manager agent]]
    SU --> J[render_draft]
    DD --> J
    J --> HF{{"editorial_review<br/>you: approve / revise / reject"}}
    HF -->|revise, max 2| RV[revise_brief] --> HF
    HF -->|approve| P["publish<br/>briefs/week-NN.md + .json, remember facts"]
    HF -->|reject| X[archive]
    Q --> P
```

1. **Mock world (inputs, $0).** `scripts/gen_world.py` writes 3 weeks of deterministic fake data: competitor
   pricing/changelog/careers HTML (served at `127.0.0.1:8765` so CrewAI's real scraping tool works), a news
   SQLite DB, and an MCP server with funding + app-rating data. Week 1 is boring, week 2 has material news
   (price cut, Series B, Kolkata hiring), week 3 has one subtle signal and one rumor to discount.
2. **`load_week`** (`@start`). Flow state (`CIState`) is restored from last week's run with `@persist` +
   `restore_from_state_id`, so the flow "remembers" last week's threats. It also recalls `/competitor/*` memories.
3. **`scout_all`**. One Scout agent per competitor runs **without a crew** (`Agent.kickoff_async`, all 3 concurrently).
   Each uses tools (scrape, news search, changelog diff, pricing parse, MCP) and must return a typed
   `SignalReport` (`response_format=`). Each scout can only see its own competitor's memory scope.
4. **`assess_materiality`** (`@router`). A deterministic score over the signals (impact × credibility × novelty)
   picks a label: `nothing_new`, `routine` or `material`. An LLM is called only to break a tie.
   The label *is* the route: listeners subscribed to that string run next.
5. **The crew that writes the draft.**
   - *routine* → **ShortUpdateCrew**: 2 agents, sequential, defined as JSON (`crew.jsonc`). It classifies threats,
     then writes a short `Brief`.
   - *material* → **DeepDiveCrew**: hierarchical. A "Head of Strategy" manager agent delegates pricing, product
     and market analyses (run in parallel), then synthesis. An escalation memo runs **only if** a threat is high
     (`ConditionalTask`), then the final `Brief`. It reads KiranaCloud's own docs as **knowledge**, and it
     checkpoints after each task, so a crash resumes without redoing finished tasks.
   - Every brief passes **guardrails** before it's accepted: every claim cites a signal ID (code check), no
     low-credibility claim stated as fact (LLM-checked rule), and length. A failure auto-retries the task.
6. **`editorial_review`** (`@human_feedback`). The flow pauses and shows *you* the draft. You type free text.
   An LLM maps it to `approve` / `revise` / `reject` (`emit=`), and with `learn=True` your feedback is
   distilled into a lesson stored in memory, so next week's draft is pre-reviewed against it.
   `revise` loops through a revision crew, at most 2 times.
7. **`publish`**. Writes `briefs/week-NN.md` + `.json`, saves key facts to memory (`/brief/week-NN`), and records
   the flow state id so next week can fork from it.
8. **Cross-cutting, always on.** `@on` hooks enforce policy (scrapes may only hit `127.0.0.1:8765`, the spend cap
   aborts runs, secrets are redacted). An event listener records every LLM call's tokens to the cost ledger
   and drives a live console.

After a few weeks have run, **`compintel chat`** (a conversational Flow) answers questions such as "What did
ShelfMitra do in Kolkata and when?" from memory and past briefs.

## Milestones

| | What lands | Est. cost |
|---|---|---|
| M0 | Config (Anthropic-only), ScriptedLLM test fake, cost ledger listener, live hello | $0.05 |
| M1 | Mock world generator, fake web server, MCP server, ground truth | $0 |
| M2 | Tools, URL-allowlist hook, Scout fan-out | $0.80 |
| M3 | ShortUpdateCrew (JSON-first, guardrails) | $0.80 |
| M4 | DeepDiveCrew (hierarchical, knowledge, planning) + process comparison | $2.00 |
| M5 | The Flow, persistence, checkpointing, replay | $1.20 |
| M6 | Human feedback (blocking, async, learn) | $1.00 |
| M7 | Unified memory + Ask the Analyst chat | $1.20 |
| M8 | Full hooks, tracing, evals, `crewai test/train` | $3.00 |
| M9 | Polish & retrospective | $0.30 |

Budget cap $12, enforced in code from M0.

## Resetting local state

`crewai reset-memories -a` clears CrewAI's memory/knowledge stores. `rm -rf runs/` clears flow persistence,
checkpoints and the cost ledger (don't do this if you want to keep the spend history).
