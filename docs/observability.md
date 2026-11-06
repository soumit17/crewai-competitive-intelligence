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

Generated by `uv run compintel traces --write` from the local Phoenix (the developer's runs).

<!-- trace-report:start -->
*Not generated yet: run the M8b handoff commands, then `uv run compintel traces --write`.*
<!-- trace-report:end -->
