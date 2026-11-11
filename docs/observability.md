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

Generated by `uv run compintel traces --write` from the local Phoenix: every run the developer traced, failures
included. 2026-11-06/08: the $0 capped aborts, weeks 1-3 with console reviews (all answered empty, see below),
the `manager_agent` deep dive, three week-1 starts stopped early. 2026-11-11: the published weeks, run with
`--review async` (week 1 `dbe310ac`, week 2 `10ff5416` crashed in the planner then `8baf976b` resumed, week 3
`f36ac218` forked the old week 2 and was set aside), each answered with `compintel review <id> "approve"` in
its own traced process. What it shows is summarized after the block.

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
| `a7338221` | CompetitiveIntelFlow.kickoff | 2026-11-08T21:19:22 | 711 | 177 | 42 | 205,398 |
| `b8e2ac98` | CompetitiveIntelFlow.kickoff | 2026-11-08T21:36:13 | 307 | 242 | 73 | 432,684 |
| `f5601c64` | CompetitiveIntelFlow.kickoff | 2026-11-08T21:43:48 | 155 | 169 | 32 | 221,285 |
| `3c734b5d` | CompetitiveIntelFlow.kickoff | 2026-11-08T21:52:09 | 8 | 68 | 12 | 13,200 |
| `29cfd36b` | CompetitiveIntelFlow.kickoff | 2026-11-08T21:52:25 | 8 | 68 | 12 | 13,200 |
| `e6260674` | CompetitiveIntelFlow.kickoff | 2026-11-08T21:53:16 | 8 | 68 | 12 | 13,200 |
| `dbe310ac` | CompetitiveIntelFlow.kickoff | 2026-11-11T16:20:50 | 81 | 45 | 22 | 50,574 |
| `10ff5416` | CompetitiveIntelFlow.kickoff | 2026-11-11T16:53:06 | 155 | 109 | 23 | 217,495 |
| `f36ac218` | CompetitiveIntelFlow.kickoff | 2026-11-11T16:55:58 | 83 | 116 | 20 | 188,077 |
| `8baf976b` | CompetitiveIntelFlow.kickoff | 2026-11-11T17:03:24 | 244 | 121 | 45 | 274,774 |
| `62c2ebe5` | CompetitiveIntelFlow.kickoff | 2026-11-11T17:16:25 | 87 | 114 | 18 | 161,762 |
| `998d9811` | CompetitiveIntelFlow.kickoff | 2026-11-11T17:28:52 | 87 | 56 | 31 | 73,904 |
| `119824ca` | CompetitiveIntelFlow.kickoff | 2026-11-11T17:31:06 | 65 | 93 | 13 | 107,335 |
| `68a46135` | CompetitiveIntelFlow.kickoff | 2026-11-11T18:26:00 | 69 | 49 | 22 | 53,130 |

### Where the money goes (per-agent token share)

| agent | LLM calls | prompt tok | output tok | share of tokens | USD |
|---|---:|---:|---:|---:|---:|
| Competitive Signal Scout | 133 | 926,042 | 134,247 | 37% | 0.1208 |
| - | 200 | 454,397 | 105,744 | 20% | 0.0983 |
| Market Analyst | 63 | 219,567 | 60,591 | 10% | 0.0523 |
| Brief Writer | 30 | 253,686 | 62,388 | 11% | 0.0480 |
| Strategist | 28 | 77,610 | 42,137 | 4% | 0.0288 |
| Head of Strategy | 11 | 90,251 | 38,005 | 4% | 0.0263 |
| Editor | 10 | 72,287 | 23,400 | 3% | 0.0183 |
| Task Execution Planner | 9 | 121,537 | 21,533 | 5% | 0.0176 |
| Pricing Analyst | 8 | 41,782 | 21,932 | 2% | 0.0134 |
| Product Analyst | 8 | 37,312 | 10,523 | 2% | 0.0074 |
| Guardrail Agent | 13 | 35,174 | 6,954 | 1% | 0.0070 |

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

### `a7338221` CompetitiveIntelFlow.kickoff

**Fan-out** (agent spans that ran at the same time):

```text
Competitive Signal Scout   |████████████████████████                |   0.00s →  18.40s
Competitive Signal Scout   | ███████████████████████████████████████|   0.99s →  31.08s
Competitive Signal Scout   |   ██████████████████                   |   1.97s →  15.66s
```
**Guardrails**: 8 checks, 2 failed → retried.
- ✗ `Judge only from the brief's own text. A claim the brief itse`: guardrail failed (attempt 2): The unverified BahiBook Maharashtra user claim (50,000 users, sourced to a single Pune Business Times report) appears outside disc
- ✗ `Judge only from the brief's own text. A claim the brief itse`: guardrail failed (attempt 1): The brief's watchlist describes BahiBook's Maharashtra user milestone (50,000 users) as a 'press-reported figure', which is an unc

**Memory and knowledge**: memory.recall ×28 (3.1s), memory.retrieve_for_task ×7 (1.8s), memory.save ×20 (59.2s)

**Human review**: waited 199s for the reviewer; answer '(empty: revise)'

**Human review**: waited 550s for the reviewer; answer ''

**Human review**: waited 55s for the reviewer; answer '(empty: revise)'

**Slowest spans**: CompetitiveIntelFlow.editorial_review 550.0s, human review editorial_review 549.9s, CompetitiveIntelFlow.editorial_review 198.8s, human review editorial_review 198.7s, CompetitiveIntelFlow.run_short_update 64.2s

### `b8e2ac98` CompetitiveIntelFlow.kickoff

**Fan-out** (agent spans that ran at the same time):

```text
Competitive Signal Scout   |██████████████████                      |   0.00s →  19.20s
Competitive Signal Scout   | ███████████████████████████████████████|   1.02s →  42.86s
Competitive Signal Scout   |  █████████████                         |   2.02s →  15.43s
```
```text
Market Analyst             |████████████████████████████████████████|   0.00s →  59.12s
Pricing Analyst            |██████████████████                      |   0.39s →  27.38s
Product Analyst            | █████████                              |   0.98s →  14.57s
```
**Guardrails**: 8 checks, 1 failed → retried.
- ✗ `brief_length_ok`: guardrail failed (attempt 1): The brief renders to 651 words; cut it to at most 600.

**Memory and knowledge**: knowledge.query ×7 (15.3s), knowledge.search ×7 (16.2s), memory.recall ×21 (2.3s), memory.retrieve_for_task ×5 (1.2s), memory.save ×22 (61.1s)

**Human review**: waited 63s for the reviewer; answer '(empty: revise)'

**Human review**: waited 63s for the reviewer; answer '(empty: revise)'

**Human review**: waited 5s for the reviewer; answer '(empty: revise)'

**Slowest spans**: CompetitiveIntelFlow.run_deep_dive 185.0s, DeepDiveCrew.kickoff 183.8s, CompetitiveIntelFlow.editorial_review 63.1s, human review editorial_review 63.0s, CompetitiveIntelFlow.editorial_review 62.6s

### `f5601c64` CompetitiveIntelFlow.kickoff

**Fan-out** (agent spans that ran at the same time):

```text
Competitive Signal Scout   |██████████████████████████████          |   0.00s →  29.68s
Competitive Signal Scout   | ███████████████████████████████████    |   1.02s →  35.20s
Competitive Signal Scout   |  ██████████████████████████████████████|   2.03s →  39.09s
```
**Guardrails**: 6 checks, 0 failed → retried.

**Memory and knowledge**: memory.recall ×28 (2.9s), memory.retrieve_for_task ×5 (1.1s), memory.save ×21 (34.8s)

**Human review**: waited 1s for the reviewer; answer '(empty: revise)'

**Human review**: waited 3s for the reviewer; answer '(empty: revise)'

**Human review**: waited 21s for the reviewer; answer '(empty: revise)'

**Slowest spans**: CompetitiveIntelFlow.scout_all 41.9s, Competitive Signal Scout.kickoff 37.1s, CompetitiveIntelFlow.publish 36.1s, CompetitiveIntelFlow.run_short_update 34.7s, Short Update Crew.kickoff 34.2s

### `3c734b5d` CompetitiveIntelFlow.kickoff

**Fan-out** (agent spans that ran at the same time):

```text
Competitive Signal Scout   |████████████████████████████████████████|   0.00s →   4.83s
Competitive Signal Scout   |        ████████████████████████████████|   0.94s →   4.84s
Competitive Signal Scout   |               ████████████████████████ |   1.87s →   4.81s
```
**Memory and knowledge**: memory.recall ×4 (0.6s), memory.retrieve_for_task ×3 (0.5s), memory.save ×3 (0.7s)

**Human review**: waited 1s for the reviewer; answer 'approve'

**Slowest spans**: CompetitiveIntelFlow.scout_all 6.1s, Competitive Signal Scout.kickoff 4.8s, Competitive Signal Scout.kickoff 3.9s, CompetitiveIntelFlow.load_knowledge_digest 3.1s, Competitive Signal Scout.kickoff 2.9s

### `29cfd36b` CompetitiveIntelFlow.kickoff

**Fan-out** (agent spans that ran at the same time):

```text
Competitive Signal Scout   |████████████████████████████████████████|   0.00s →   4.86s
Competitive Signal Scout   |       █████████████████████████████████|   0.91s →   4.90s
Competitive Signal Scout   |               █████████████████████████|   1.84s →   4.88s
```
**Memory and knowledge**: memory.recall ×4 (0.6s), memory.retrieve_for_task ×3 (0.5s), memory.save ×3 (0.8s)

**Human review**: waited 1s for the reviewer; answer 'approve'

**Slowest spans**: CompetitiveIntelFlow.scout_all 6.1s, Competitive Signal Scout.kickoff 4.9s, Competitive Signal Scout.kickoff 4.0s, CompetitiveIntelFlow.load_knowledge_digest 3.1s, Competitive Signal Scout.kickoff 3.0s

### `e6260674` CompetitiveIntelFlow.kickoff

**Fan-out** (agent spans that ran at the same time):

```text
Competitive Signal Scout   |████████████████████████████████████████|   0.00s →   4.97s
Competitive Signal Scout   |       ████████████████████████████████ |   0.93s →   4.95s
Competitive Signal Scout   |               █████████████████████████|   1.85s →   4.96s
```
**Memory and knowledge**: memory.recall ×4 (0.6s), memory.retrieve_for_task ×3 (0.5s), memory.save ×3 (0.8s)

**Human review**: waited 1s for the reviewer; answer 'approve'

**Slowest spans**: CompetitiveIntelFlow.scout_all 6.2s, Competitive Signal Scout.kickoff 5.0s, Competitive Signal Scout.kickoff 4.0s, Competitive Signal Scout.kickoff 3.1s, CompetitiveIntelFlow.load_knowledge_digest 3.1s

### `dbe310ac` CompetitiveIntelFlow.kickoff

**Memory and knowledge**: memory.save ×20 (73.5s)

**Slowest spans**: CompetitiveIntelFlow.publish 75.7s, memory.save 5.9s, memory.save 5.2s, claude-haiku-5-5.llm_call 5.0s, memory.save 4.6s

### `10ff5416` CompetitiveIntelFlow.kickoff

**Fan-out** (agent spans that ran at the same time):

```text
Competitive Signal Scout   |██████████████████████████████          |   0.00s →  51.06s
Competitive Signal Scout   | ███████████████████                    |   0.99s →  33.11s
Competitive Signal Scout   | ███████████████████████████████████████|   1.97s →  67.33s
```
**Memory and knowledge**: memory.recall ×20 (2.4s), memory.retrieve_for_task ×3 (0.6s)

**Slowest spans**: CompetitiveIntelFlow.run_deep_dive 84.8s, DeepDiveCrew.kickoff 83.2s, CompetitiveIntelFlow.scout_all 70.6s, Competitive Signal Scout.kickoff 65.4s, Competitive Signal Scout.kickoff 51.1s

### `f36ac218` CompetitiveIntelFlow.kickoff

**Fan-out** (agent spans that ran at the same time):

```text
Competitive Signal Scout   |█████████████████████████████           |   0.00s →  28.81s
Competitive Signal Scout   | ███████████████████████████████████████|   1.00s →  39.81s
Competitive Signal Scout   |  ███████████████████████████           |   1.97s →  29.21s
```
**Guardrails**: 4 checks, 0 failed → retried.

**Memory and knowledge**: memory.recall ×23 (2.9s), memory.retrieve_for_task ×5 (1.3s)

**Slowest spans**: CompetitiveIntelFlow.scout_all 42.6s, CompetitiveIntelFlow.run_short_update 39.6s, Short Update Crew.kickoff 39.4s, Competitive Signal Scout.kickoff 38.8s, Competitive Signal Scout.kickoff 28.8s

### `8baf976b` CompetitiveIntelFlow.kickoff

**Fan-out** (agent spans that ran at the same time):

```text
Product Analyst            |████████                                |   0.00s →  13.07s
Pricing Analyst            |███████████████                         |   0.72s →  25.13s
Market Analyst             |████████████████████████████████████████|   0.82s →  66.27s
```
**Guardrails**: 6 checks, 1 failed → retried.
- ✗ `brief_length_ok`: guardrail failed (attempt 1): The brief renders to 671 words; cut it to at most 600.

**Memory and knowledge**: knowledge.query ×7 (16.6s), knowledge.search ×7 (17.6s), memory.recall ×7 (0.9s), memory.retrieve_for_task ×2 (0.8s)

**Slowest spans**: CompetitiveIntelFlow.run_deep_dive 242.3s, DeepDiveCrew.kickoff 241.1s, Market Analyst.market_threats.execute 65.5s, Task Execution Planner.Based on these tasks summar 50.9s, claude-haiku-5-5.llm_call 50.8s

### `62c2ebe5` CompetitiveIntelFlow.kickoff

**Fan-out** (agent spans that ran at the same time):

```text
Competitive Signal Scout   |████████████████████████████            |   0.00s →  22.79s
Competitive Signal Scout   | ███████████████████████████████████████|   1.01s →  32.71s
Competitive Signal Scout   |  ███████████████████████               |   2.01s →  20.60s
```
**Guardrails**: 5 checks, 1 failed → retried.
- ✗ `low_credibility_only_discounted`: guardrail failed (attempt 1): Fix and return the whole Brief again: StockSathi-3-8 (credibility < 0.5) is used as fact outside discounted_claims

**Memory and knowledge**: memory.recall ×24 (2.8s), memory.retrieve_for_task ×6 (1.6s)

**Slowest spans**: CompetitiveIntelFlow.run_short_update 50.8s, Short Update Crew.kickoff 50.6s, CompetitiveIntelFlow.scout_all 35.5s, Competitive Signal Scout.kickoff 31.7s, Competitive Signal Scout.kickoff 22.8s

### `998d9811` CompetitiveIntelFlow.kickoff

**Memory and knowledge**: memory.save ×22 (81.4s)

**Slowest spans**: CompetitiveIntelFlow.publish 79.6s, memory.save 5.2s, memory.save 5.2s, memory.save 4.9s, claude-haiku-5-5.llm_call 4.7s

### `119824ca` CompetitiveIntelFlow.kickoff

**Fan-out** (agent spans that ran at the same time):

```text
Competitive Signal Scout   |████████████████████████████████████████|   0.00s →  28.22s
Competitive Signal Scout   | █████████████████████████████████████  |   0.99s →  27.14s
Competitive Signal Scout   |   ██████████████████                   |   1.98s →  14.80s
```
**Guardrails**: 4 checks, 0 failed → retried.

**Memory and knowledge**: memory.recall ×15 (1.8s), memory.retrieve_for_task ×5 (1.2s)

**Slowest spans**: CompetitiveIntelFlow.run_short_update 33.6s, Short Update Crew.kickoff 33.4s, CompetitiveIntelFlow.scout_all 31.0s, Competitive Signal Scout.kickoff 28.2s, Competitive Signal Scout.kickoff 26.2s

### `68a46135` CompetitiveIntelFlow.kickoff

**Memory and knowledge**: memory.save ×24 (63.3s)

**Slowest spans**: CompetitiveIntelFlow.publish 61.0s, memory.save 6.4s, claude-haiku-5-5.llm_call 6.3s, memory.save 4.7s, memory.save 4.7s
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

