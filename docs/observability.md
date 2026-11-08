# Observability: streaming and tracing (M8b)

Three ways to watch a CompIntel run, from quickest to deepest:

| | What you get | When |
|---|---|---|
| Live panel (M8a, default in a terminal) | One box: current step, task, agent, counts, spend | Every run |
| `--stream` | Steps, tasks, tool calls and the models' text as it is generated | Watching a run's reasoning as it happens |
| `--trace` → Phoenix | The whole run as a tree you can click through afterwards: every agent, LLM call (with prompt, answer and tokens), tool, guardrail, memory operation, hook intervention and the human review | Debugging, cost questions, this document |

## Setup

Phoenix runs as its own app next to CompIntel, in an isolated environment `uvx` builds the first time (a few
hundred MB, once):

```bash
uvx --from arize-phoenix==21.0.0 phoenix serve      # → http://127.0.0.1:6006, data in ~/.phoenix
```

It is not a dependency of this project: Phoenix 21 needs `aiosqlite>=0.22.1` and crewai 1.15.33 pins
`<0.22`, and a server doesn't belong in the app's environment anyway. CompIntel's venv only has the client
side (`openinference-instrumentation-crewai`, `arize-phoenix-otel`, `arize-phoenix-client`, ~3 MB on top of
the OpenTelemetry SDK crewai already ships).

`--trace` works on `compintel run`, `review` and `deepdive`. Each traced run prints a link per trace; every
trace goes to the Phoenix project `compintel`. `uv run compintel traces` summarizes what Phoenix holds ($0).

## How a crewai run becomes a trace

```
crewai event bus ──► OpenInferenceEventListener ──► OpenTelemetry spans ──► OTLP/HTTP ──► Phoenix
 (every step emits       (+ CompintelSpans,            (one trace per run,
  start/end events)       src/compintel/tracing.py)     nested by parent_event_id)
```

crewai already emits a start and an end event for everything it does, and every event carries
`parent_event_id` (the scope it happened in) and, for an end event, `started_event_id`. OpenInference's crewai
package has two modes; we use the **event-listener mode**, which turns those events into spans and nests them by
those ids. Its default mode instead wraps crewai's methods (`Crew.kickoff`, `Task._execute_core`, ...), which
our rule against patching crewai rules out when a native hook exists.

The listener covers flows, crews, agents, LLM calls and tools. `CompintelSpans` adds the rest of what our
runs do, with the listener's own span assembler so it all lands in one tree:

| Span name | Kind | From |
|---|---|---|
| `CompetitiveIntelFlow.kickoff` → `CompetitiveIntelFlow.<step>` | CHAIN | the listener (flow, steps) |
| `Short Update Crew.kickoff`, `DeepDiveCrew.kickoff` | CHAIN | the listener |
| `<Role>.<task>.execute` (e.g. `Brief Writer.write_short_brief.execute`) | AGENT | the listener; a task has no span of its own, its agent's span carries the task name |
| `claude-haiku-5-5.llm_call` | LLM | the listener (prompt, answer, token counts); **we add `compintel.agent` and `compintel.task`** |
| `<tool>.run` (e.g. `render_brief.run`, `delegate_work_to_coworker.run`) | TOOL | the listener |
| `guardrail <name>` | GUARDRAIL | ours; **ERROR when it failed** (the task goes back to the agent) |
| `hook <point> <outcome>` | GUARDRAIL | ours; only `modified` / `aborted` dispatches (a run has hundreds that just proceeded) |
| `memory.save`, `memory.recall`, `memory.retrieve_for_task` | CHAIN / RETRIEVER | ours |
| `knowledge.search` → `knowledge.query` | RETRIEVER / CHAIN | ours (the deep dive's crew knowledge) |
| `mcp.connect <server>`, `mcp.tool <name>` | CHAIN / TOOL | ours (the scouts' market-signals server) |
| `reasoning <role>` | CHAIN | ours (agents with `reasoning=True`) |
| `human review editorial_review` | CHAIN | ours; from the request to the answer, so its length is how long the run waited on you |

**Why every scope, not just the interesting ones.** The assembler parks a span until its parent's span exists.
A guardrail opens a crewai scope; the rumor guardrail's judge makes an LLM call inside it. Without a
`guardrail` span that LLM call would wait for a parent that never comes, and never be exported: not a missing
label, a missing call (and its tokens). `test_the_llm_call_inside_a_guardrail_is_exported_under_it` pins this.

## The six things to look for

Every excerpt below is from a scripted ($0) run in `tests/test_tracing.py`, printed as a tree (status OK unless
marked). The live versions are in the next section.

### 1. A hierarchical deep dive: manager delegations

`deepdive --variant manager_agent`. Each task is the manager's; the manager hands it on with crewai's
delegation tool, and the specialist's own agent span sits *inside* that tool call:

```text
DeepDiveCrew.kickoff
  Head of Strategy.pricing_analysis.execute        [AGENT]
    claude-haiku-5-5.llm_call                      [LLM]   the manager decides to delegate
    delegate_work_to_coworker.run                  [TOOL]
      knowledge.search → knowledge.query → llm_call         the specialist's knowledge lookup
      Pricing Analyst.Analyse this week's competitor pricing.execute   [AGENT]
        claude-haiku-5-5.llm_call                  [LLM]
    claude-haiku-5-5.llm_call                      [LLM]   the manager reads the result
  Head of Strategy.product_overlap.execute ...     → Product Analyst
  Head of Strategy.market_threats.execute ...      → Market Analyst (with a `reasoning` span: it plans first)
```

`compintel traces` lists them one per line: `Head of Strategy → delegate_work_to_coworker → Pricing Analyst`,
with how long each delegation took.

### 2. An async-task fan-out

`run --week 2 --variant sequential`: the three analyses are `async_execution: true`, so their agent spans are
siblings that overlap in time, and synthesis starts only when all three are done:

```text
DeepDiveCrew.kickoff
  Product Analyst.product_overlap.execute         ┐
  Market Analyst.market_threats.execute           ├ started together
  Pricing Analyst.pricing_analysis.execute        ┘
  guardrail threat_levels_consistent
  Strategist.synthesis.execute
```

Phoenix's timeline view shows the overlap; `compintel traces` draws it. From the scripted run (each fake
call sleeps 50 ms; the live report below has real durations):

```text
Market Analyst         |████████████████████████████████████████|   0.00s →   0.12s
Product Analyst        | █████████████████████████              |   0.01s →   0.09s
Pricing Analyst        |    ███████████████████████             |   0.01s →   0.09s
```

(The three scouts in `scout_all` run concurrently too, but as `Agent.kickoff_async` calls, not crew tasks.)

### 3. A guardrail retry

A draft whose TL;DR cites no signal: the function guardrail fails (ERROR, with its message), the writer runs
again with that message as feedback, and the retry passes:

```text
Short Update Crew.kickoff
  Brief Writer.write_short_brief.execute
  guardrail Judge only from the brief's own text. ...    the LLM guardrail
    Guardrail Agent.kickoff → claude-haiku-5-5.llm_call
  guardrail every_claim_cites_signal          ERROR  "guardrail failed (attempt 1): tl_dr[0] cites no signal id"
  Brief Writer.write_short_brief.execute               the retry
  guardrail every_claim_cites_signal          OK
  guardrail low_credibility_only_discounted
```

The LLM guardrail isn't re-run on the retry: crewai re-checks a retry only with the guardrail that asked for it
(an M3 finding, visible here as one `Guardrail Agent` call).

### 4. A hook abort

The budget gate at `EXECUTION_START`. To see one live for $0, lower the cap below what's been spent:

```bash
P3_SPEND_CAP_USD=1 uv run compintel run --week 1 --trace
# hooks 1 dispatches: 0 proceeded, 0 modified, 1 aborted
#   abort budget-gate: P3 spend cap reached: $1.3598 of $1.00
# stopped by a hook (budget-gate): P3 spend cap reached: $1.3598 of $1.00
```

```text
hook execution_start aborted     ERROR  "budget-gate: P3 spend cap reached: $1.3598 of $1.00"
                                        attributes: hook.point, hook.outcome, hook.abort_source=budget-gate
```

It is a trace of its own: crewai dispatches `EXECUTION_START` before the flow opens its scope, so the
dispatch has no parent. A `PRE_MODEL_CALL` abort (the iteration guard, the deep dive's input check) lands
inside the agent span whose call it stopped.

### 5. Memory save and recall

```text
CompetitiveIntelFlow.editorial_review
  memory.recall                        lessons from earlier reviews (learn=True), before you see the draft
  human review editorial_review        output: your answer
  claude-haiku-5-5.llm_call            maps your answer to approve / revise / reject
CompetitiveIntelFlow.publish
  memory.save ×3                       the week's signals and brief facts (/brief/week-NN)
  claude-haiku-5-5.llm_call            memory's own analysis of what to save
Short Update Crew.kickoff
  memory.retrieve_for_task             the writer's memory view, before each task
    memory.recall ×3
```

### 6. Per-agent token share: where the money goes

Every LLM span carries `llm.token_count.prompt / completion / total`, `llm.model_name` and (ours)
`compintel.agent`. `compintel traces` groups them and prices them with `compintel.cost.PRICES`. Agent `-`
means a call made outside any agent: memory's analysis, the router's tie-break, the review classifier. The
cost ledger (`compintel costs`) counts the same calls from crewai's `LLMCallCompletedEvent`.

## Streaming (`--stream`)

`compintel run --week N --stream` uses crewai's Flow streaming: with `flow.stream = True`, `Flow.kickoff()`
returns a `StreamSession` instead of the result. The flow runs on a background thread, and iterating the
session yields a `StreamFrame` for every event the run emits (channels `llm`, `flow`, `tools`, `messages`,
`lifecycle`, `custom`); `.result` holds the flow's return value at the end. `StreamPrinter`
(`src/compintel/streaming.py`) prints the frames worth a line. From the scripted run in
`tests/test_streaming.py` (lines cut at 100 characters):

```text
▶ run_short_update
  • crew kickoff started: Short Update Crew
  • task started: classify_signals
    Market Analyst classify_signals
{"threats":[{"company":"StockSathi","threat":"medium","rationale":"Rating dip [StockSathi-3-1]","sig
  • task started: write_short_brief
    Brief Writer write_short_brief
{"week": 3, "headline": "BahiBook eyes GST e-invoicing", "tl_dr": ["BahiBook is hiring a GST e-invoi
    Guardrail Agent
{"valid":true,"feedback":null}
  • task started: render_draft
    Brief Writer render_draft
    ⇢ calls render_brief(…)
    → tool render_brief
✓ run_short_update 0.4s
```

Things worth knowing:

- **Token frames need streaming LLMs.** The Flow's stream carries whatever events happen; an LLM emits
  `LLMStreamChunkEvent`s only with `stream=True`. `--stream` sets that for every LLM built afterwards
  (`config.stream_tokens`). `Crew.stream=True` would do it per crew (crewai's `enable_agent_streaming`), but it
  also makes `crew.kickoff()` return a `CrewStreamingOutput`, which the flow steps would have to drain; one
  stream, the Flow's, is enough.
- **Parallel calls interleave.** The three scouts and the parallel analysts stream at the same time; the
  printer starts a new header (`Market Analyst classify_signals`) whenever the call changes.
- **Structured answers stream as JSON**, tool calls by name first (the arguments follow as they're
  generated; the printer shows only the name).
- **The live panel is off** while streaming (it would redraw over the text). The console review still works:
  crewai asks for your feedback from the flow's thread while the main thread waits on the stream.
- **Not streamed**: the review classifier (`flow.REVIEW_LLM`, built when `flow.py` is imported, before the
  flag is read) and memory's analysis model (crewai sets `stream=False` on it itself).
- **Direct calls**: `uv run python scripts/stream_demo.py` streams three plain `LLM.call`s (text, a tool call,
  a structured answer) and prints the chunk events as they come. Each `LLMStreamChunkEvent` carries `chunk`,
  `tool_call` (`id`, `function.name`, `function.arguments` so far, `index`), `call_type`, `response_id`,
  `call_id` and, inside a crew, `agent_role` / `task_name`.

## Live runs: weeks 1-3 under tracing

Generated by `uv run compintel traces --write` from the local Phoenix (the developer's runs on 2026-11-08:
the $0 capped abort, week 1 with `--stream`, week 2 `--variant sequential`, and the `manager_agent` deep
dive on week 2's reports; week 3 not yet). What it shows is summarized after the block.

<!-- trace-report:start -->
| trace | root | started (UTC) | seconds | spans | LLM calls | tokens |
|---|---|---|---:|---:|---:|---:|
| `5aab60b8` | hook execution_start aborted | 2026-11-06T13:13:02 | 0 | 1 | 0 | 0 |
| `0094c201` | CompetitiveIntelFlow.kickoff | 2026-11-06T13:13:02 | 0 | 1 | 0 | 0 |
| `23e3e898` | hook execution_start aborted | 2026-11-08T20:40:06 | 0 | 1 | 0 | 0 |
| `133e5156` | CompetitiveIntelFlow.kickoff | 2026-11-08T20:40:06 | 0 | 1 | 0 | 0 |
| `a0ad5ccd` | CompetitiveIntelFlow.kickoff | 2026-11-08T20:40:36 | 146 | 150 | 31 | 139,002 |
| `10cc57ba` | CompetitiveIntelFlow.kickoff | 2026-11-08T20:43:41 | 361 | 247 | 75 | 430,924 |
| `9cceb4f1` | DeepDiveCrew.kickoff | 2026-11-08T20:58:32 | 285 | 84 | 30 | 261,155 |

### Where the money goes (per-agent token share)

| agent | LLM calls | prompt tok | output tok | share of tokens | USD |
|---|---:|---:|---:|---:|---:|
| Head of Strategy | 11 | 90,251 | 38,005 | 15% | 0.0263 |
| - | 41 | 132,374 | 22,148 | 19% | 0.0243 |
| Competitive Signal Scout | 26 | 160,153 | 26,775 | 22% | 0.0219 |
| Market Analyst | 19 | 67,506 | 18,706 | 10% | 0.0161 |
| Strategist | 16 | 44,324 | 17,828 | 7% | 0.0133 |
| Task Execution Planner | 3 | 58,745 | 12,149 | 9% | 0.0094 |
| Editor | 4 | 28,211 | 9,276 | 5% | 0.0072 |
| Pricing Analyst | 4 | 19,588 | 11,627 | 4% | 0.0070 |
| Brief Writer | 4 | 31,214 | 8,291 | 5% | 0.0064 |
| Product Analyst | 4 | 16,134 | 5,321 | 3% | 0.0036 |
| Guardrail Agent | 4 | 10,854 | 1,601 | 1% | 0.0019 |

### `5aab60b8` hook execution_start aborted

**Hook interventions**:

- `hook execution_start aborted` budget-gate: P3 spend cap reached: $1.3598 of $1.00


### `0094c201` CompetitiveIntelFlow.kickoff

Nothing ran: the run was stopped before its first step (see the hook abort just above).

### `23e3e898` hook execution_start aborted

**Hook interventions**:

- `hook execution_start aborted` budget-gate: P3 spend cap reached: $1.3601 of $1.00


### `133e5156` CompetitiveIntelFlow.kickoff

Nothing ran: the run was stopped before its first step (see the hook abort just above).

### `a0ad5ccd` CompetitiveIntelFlow.kickoff

**Fan-out** (agent spans that ran at the same time):

```text
Competitive Signal Scout   |███████████████████████████████████     |   0.00s →  28.84s
Competitive Signal Scout   | ███████████████████████████████████████|   0.99s →  33.29s
Competitive Signal Scout   |  ██████████████████                    |   1.98s →  17.31s
```
**Guardrails**: 6 checks, 0 failed → retried.

**Memory and knowledge**: memory.recall ×22 (3.2s), memory.retrieve_for_task ×5 (2.0s), memory.save ×19 (45.0s)

**Human review**: waited 2s for the reviewer; answer '(empty: revise)'

**Human review**: waited 5s for the reviewer; answer '(empty: revise)'

**Human review**: waited 11s for the reviewer; answer '(empty: revise)'

**Slowest spans**: CompetitiveIntelFlow.publish 43.9s, CompetitiveIntelFlow.scout_all 37.0s, Competitive Signal Scout.kickoff 32.3s, Competitive Signal Scout.kickoff 28.8s, CompetitiveIntelFlow.run_short_update 26.4s

### `10cc57ba` CompetitiveIntelFlow.kickoff

**Fan-out** (agent spans that ran at the same time):

```text
Competitive Signal Scout   |██████████████████████████              |   0.00s →  27.35s
Competitive Signal Scout   | ███████████████████████                |   0.99s →  24.88s
Competitive Signal Scout   |  ██████████████████████████████████████|   1.97s →  41.29s
```
```text
Market Analyst             |████████████████████████████████████████|   0.00s →  64.61s
Pricing Analyst            |██████████████                          |   0.58s →  22.75s
Product Analyst            |█████████                               |   0.80s →  15.78s
```
**Guardrails**: 7 checks, 0 failed → retried.

**Memory and knowledge**: knowledge.query ×6 (14.3s), knowledge.search ×6 (15.1s), memory.recall ×25 (2.9s), memory.retrieve_for_task ×4 (0.8s), memory.save ×21 (35.2s)

**Human review**: waited 259s for the reviewer; answer '(empty: revise)'

**Human review**: waited 105s for the reviewer; answer '(empty: revise)'

**Human review**: waited 110s for the reviewer; answer '(empty: revise)'

**Slowest spans**: CompetitiveIntelFlow.editorial_review 259.2s, human review editorial_review 259.1s, CompetitiveIntelFlow.run_deep_dive 206.4s, DeepDiveCrew.kickoff 205.1s, CompetitiveIntelFlow.editorial_review 110.4s

### `9cceb4f1` DeepDiveCrew.kickoff

**Manager delegations** (crewai's delegation tool → the coworker's agent span inside it):

- Head of Strategy → `delegate_work_to_coworker` → **Product Analyst** (13.2s)
- Head of Strategy → `delegate_work_to_coworker` → **Pricing Analyst** (33.4s)

**Guardrails**: 8 checks, 3 failed → retried.
- ✗ `brief_length_ok`: guardrail failed (attempt 2): The brief renders to 614 words; cut it to at most 600.
- ✗ `brief_length_ok`: guardrail failed (attempt 1): The brief renders to 643 words; cut it to at most 600.
- ✗ `Judge only from the brief's own text. A claim the brief itse`: guardrail failed (attempt 1): The brief violates the guardrail. Its recommendations state that 'the ShelfMitra hires are signal-based and not changelog-verified

**Memory and knowledge**: knowledge.query ×11 (25.8s), knowledge.search ×11 (27.0s)

**Slowest spans**: Head of Strategy.pricing_analysis.execute 63.2s, delegate_work_to_coworker.run 33.4s, Pricing Analyst.Week 2 pricing analysis of competi 30.9s, Head of Strategy.product_overlap.execute 28.8s, claude-haiku-5-5.llm_call 28.6s
<!-- trace-report:end -->

### What the live traces showed

- **All six spec items appear in real runs.** Fan-out: the three scouts (staggered ~1 s) and week 2's three
  analysts. Delegations: the `manager_agent` deep dive. Guardrail retries: three in that same run. Hook abort:
  the capped run. Memory: every week. Token share: the table.
- **The slowest analyst sets the pace.** Week 2's analysts ran side by side: Pricing 22 s, Product 16 s,
  Market 65 s. Synthesis waited for the Market Analyst, who plans first (`reasoning=True`), so parallelism
  saved ~38 s, not the full 54 s.
- **The manager delegated 2 of 6 tasks** (Product Analyst, Pricing Analyst) and did the rest itself. That
  matches the M4/M5 finding that a Haiku manager prefers to answer itself. Its brief then failed
  `brief_length_ok` twice (643, then 614 words, against 600) and the rumor guardrail once, all visible as red
  `guardrail` spans followed by a retry.
- **Where the money goes.** The Head of Strategy is the most expensive agent ($0.026 for 11 calls: long
  prompts holding every coworker's output). Calls outside any agent come second ($0.024 over 41 calls): memory's
  save analysis, the review classifier, the router. The scouts use the most tokens (22%) but are cheaper per
  token: mostly prompt, little output.
- **Memory saving is a routine week's slowest step.** Week 1's `publish` took 44 s, almost all of it
  19 `memory.save` spans (each one an LLM analysis plus an embedding), longer than scouting (37 s).
- **The trace and the ledger agree.** The report prices 136 LLM calls at $0.1374; the cost ledger has 133
  calls and $0.1379 for the same runs. One of the trace's calls failed: the deep dive's crew planner ran out
  of tokens mid-JSON. crewai reports usage only for completed calls, so tokens Anthropic billed for a failed
  call are in neither count.
- **All six reviews were an empty answer** (spans of 2 s to 259 s ending in `(empty: revise)`), so weeks 1
  and 2 both hit the revision limit and published with an editor's note. crewai's console prompt said "Press
  Enter to skip", while this review treats an empty answer as "revise". `ConsoleReview` now prints its own
  prompt ("Enter alone = revise") and asks once more before an empty answer counts.

