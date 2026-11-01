# Decisions & API verification (P3)

Verified 2026-10-30 against the **installed source** (`.venv/lib/python3.12/site-packages/crewai*`) and
`https://docs.crewai.com/llms.txt` (served as v1.15.33). Re-check before each milestone (CrewAI ships weekly).

## Versions

| Package | Pin | Note |
|---|---|---|
| Python | 3.12 (via uv) | crewai requires `<3.14`; the system Python is 3.14, so uv provides 3.12 |
| crewai[anthropic] | 1.15.33 | Native Anthropic provider; brings `anthropic==0.73.0`. **No** `litellm` installed |
| crewai-tools | 1.15.33 | Same release train as crewai |
| crewai-cli / crewai-core | 1.15.33 | Pulled in by crewai; the CLI now lives in the separate `crewai_cli` package |
| mcp | 1.30.0 | What crewai resolves; latest mcp is 2.3.0 but we stay on crewai's choice |
| rich | 14.3.4 | crewai → instructor caps `rich<15` |
| typer 0.27.3, pydantic-settings 2.16.2, beautifulsoup4 4.15.0 | latest | |
| dev: pytest 9.2.0, pytest-asyncio 1.4.1, pytest-cov 7.1.0, pytest-socket 0.8.1, ruff 0.17.3, pyright 1.1.414 | latest | Same as P2 |

Deferred until the milestone that needs them (spec §19.2): `fastapi`+`uvicorn` (M6 async review page),
`arize-phoenix` + `openinference-instrumentation-crewai` (M8). Embedder backend: see open question below.

Note: `openai==2.54.0` is installed as a **core** crewai dependency (even without litellm). Installed ≠ used;
the "no OpenAI" guarantee comes from configuration + `test_no_openai.py`, not from the package being absent.

## API verification (everything M0–M2 uses + spec-wide names)

| Spec name | Status in 1.15.33 | Import |
|---|---|---|
| `anthropic/<model>` strings | ✅ native provider `crewai/llms/providers/anthropic/completion.py` | `from crewai import LLM` |
| `Agent.kickoff` / `kickoff_async` + `response_format` | ✅ `kickoff(messages, response_format=None, input_files=None, from_checkpoint=None)` | `crewai.Agent` |
| Agent fields `mcps`, `memory`, `planning_config`, `knowledge_sources`, `embedder`, `max_iter/rpm/execution_time` | ✅ all present (`reasoning` still exists, superseded by `planning_config`) | |
| `BaseTool`, `@tool` | ✅ | `crewai.tools` |
| `@on`, `InterceptionPoint`, `HookAborted`, `clear_all_hooks` | ✅ points: EXECUTION_START, INPUT, OUTPUT, EXECUTION_END, PRE/POST_MODEL_CALL, PRE/POST_TOOL_CALL, PRE/POST_STEP | `crewai.hooks` |
| `HookDispatchedEvent` | ⚠️ exists but **not re-exported** from `crewai.events` | `crewai.events.types.hook_events` |
| `BaseEventListener`, `crewai_event_bus`, `LLMCallCompletedEvent`, `ToolUsageFinishedEvent`, `TaskCompletedEvent`, `MethodExecutionFinishedEvent` | ✅ | `crewai.events` |
| `Flow`, `start`, `listen`, `router`, `or_`, `and_` | ✅ | `crewai.flow.flow` |
| `persist`, `human_feedback`, `HumanFeedbackResult`, `HumanFeedbackPending` | ✅ | `crewai.flow` |
| `inputs.id` resume | ⚠️ **deprecated** → `restore_from_state_id` (docs: "Migrating from inputs.id") | |
| `Memory`, `MemoryScope`, `MemorySlice` | ✅ | `crewai.memory` |
| `ConditionalTask` | ✅ | `crewai.tasks.conditional_task` |
| `CrewBase`, `agent`, `task`, `crew`, `before_kickoff`, `after_kickoff` | ✅ | `crewai.project` |
| `load_crew` (JSON-first) | ✅ | `crewai.project.crew_loader` |
| `PlanningConfig`, `CheckpointConfig` | ✅ | `crewai` |
| `SqliteProvider`, `JsonProvider` | ✅ | `crewai.state.provider.*` |
| Conversational flows (`handle_turn`) | ✅ | `crewai.flow.conversational` |
| Custom LLM (`ScriptedLLM`) | ✅ subclass `crewai.BaseLLM`, implement `call(messages, tools, callbacks, available_functions, from_task, from_agent, response_model)`; optional `supports_function_calling`, `supports_stop_words`, `get_context_window_size` | `crewai.BaseLLM` |
| Anthropic prompt caching | ✅ provider stamps `cache_control: ephemeral` itself | |
| Env vars | ✅ `CREWAI_TRACING_ENABLED`, `CREWAI_DISABLE_TELEMETRY`, `OTEL_SDK_DISABLED`, `CREWAI_DISABLE_VERSION_CHECK`, `CREWAI_STORAGE_DIR`, `CREWAI_DESERIALIZE_CALLBACKS` | |

Still to verify in their milestone: async feedback provider interface + resume API (M6).

## Every place CrewAI defaults to OpenAI (Rule 3)

| # | Where (source) | Default | How we neutralise it |
|---|---|---|---|
| 1 | `utilities/llm_utils.py` `create_llm()` — any Agent/component without an `llm` | env `MODEL` → `MODEL_NAME` → `OPENAI_MODEL_NAME` → **`gpt-4.1-mini`** (`constants.DEFAULT_LLM_MODEL`) | Explicit `llm=` everywhere **plus** `MODEL=anthropic/...` in `.env` as a safety net |
| 2 | `utilities/planning_handler.py` — `Crew(planning=True)` | **`gpt-5.4-mini`** | Always pass `planning_llm=` |
| 3 | `memory/unified_memory.py` — `Memory.llm` | **`gpt-5.4-mini`** | `Memory(llm="anthropic/...")` |
| 4 | `memory/unified_memory.py` `_default_embedder()` | **OpenAI embeddings** | `Memory(embedder={local provider})` |
| 5 | `rag/chromadb/config.py` — knowledge sources' default embedding fn | **OpenAI embeddings** (`OPENAI_API_KEY`) | `embedder=` on Crew / Agent / knowledge |
| 6 | `flow/human_feedback.py` — `@human_feedback(emit=..., learn=True)` | falls back to `DEFAULT_LLM_MODEL` (docs examples use `gpt-5.4-mini`) | Always pass `llm=` |
| 7 | `crewai_cli/cli.py` — `crewai test -m` | **`gpt-5.4-mini`** | Pass `-m anthropic/...`; `Crew.test(eval_llm: str \| BaseLLM)` now accepts any LLM, so the spec's "OpenAI-only" note looks outdated → confirm in M8 |
| — | Hierarchical process without `manager_llm`/`manager_agent` | uses #1 | Custom `manager_agent` with explicit `llm` (M4) |
| — | `crewai train` evaluator | uses agent/crew LLM via `TaskEvaluator` → confirm in M8 | |

Grep used: `grep -rnE '"(gpt-…|openai/…)"' crewai/` + `DEFAULT_LLM_MODEL` users. Re-run when upgrading.

## Deviations from the spec

1. **Not scaffolded with `crewai create flow`.** The template uses hatchling, `requires-python <3.14` and an
   interactive provider prompt. The pyproject was written by hand to match P1/P2 (uv_build, exact pins, Python 3.12)
   and copies the template's `[tool.crewai] type = "flow"`. The `kickoff`/`plot` scripts `crewai run` calls are
   added in M5 together with `main.py`.
2. **Resume uses `restore_from_state_id` for forks.** `inputs={"id": ...}` is deprecated; M5 will demonstrate the
   current resume path from the migration guide rather than `inputs.id`.
3. **Haiku 5.5 output cap.** The native provider has no `claude-haiku-5` entry in its max-output table (falls back to
   32 000). Irrelevant because we always set `max_tokens` (1500), but noted.

4. **Embedder = `onnx`** (all-MiniLM-L6-v2 via chromadb, zero new deps, ~80 MB download) instead of the spec's
   `bge-small` through `sentence-transformers` (pulls torch, ~1–2 GB). Chosen at M0 kickoff.
5. **Budget gate at `PRE_MODEL_CALL`, from M0** (spec: `EXECUTION_START` in M8). `Agent.kickoff` has no crew/flow
   execution boundary, and a per-call check also stops a run that crosses the cap midway.
6. **Hooks/handlers are installed explicitly** (`listeners.install()`, `hooks.install()` from the CLI) rather than at
   import, so tests control them (`clear_all_hooks()` + `crewai_event_bus.scoped_handlers()` per test).

## M0 findings (installed source + tests)

- **Usage accounting.** The native provider's usage dict counts cache reads/writes *inside* `input_tokens`
  (`cached_prompt_tokens`, `cache_creation_tokens` are subsets). `cost.split_usage` subtracts them so each token is
  priced once. `LLMCallCompletedEvent` carries `model` (prefix stripped: `claude-haiku-5-5`), `usage`,
  `response_id`, `agent_role`.
- **The event bus runs sync handlers on a thread pool.** Anything reading the ledger right after a kickoff must call
  `crewai_event_bus.flush()` first (CLI, budget gate, tests).
- **`Agent.kickoff` returns a coroutine when an event loop is running** (i.e. inside a Flow). Sync code gets a
  `LiteAgentOutput`; inside Flow methods use `kickoff_async` (M2 Scout).
- **`response_format` with a custom LLM:** crewai passes the model class as `response_model` to `BaseLLM.call` and makes
  one call; no ReAct parsing. `LiteAgentOutput.usage_metrics` is the per-call delta.
- **Structured output on Haiku 5.5.** `NATIVE_STRUCTURED_OUTPUT_MODELS` in the provider lists `claude-sonnet-5`
  (so Sonnet 5.5 uses native JSON-schema output) but no `claude-haiku-5`, so Haiku 5.5 takes the *tool-based* fallback:
  a `structured_output` tool with forced `tool_choice: {"type": "tool"}`. **Verified live (2026-10-30): it works.**
  First `compintel hello`: 20 input + 857 cache-write + 176 output tokens = $0.000197. The provider stamped
  `cache_control` on its own, so the agent's system prompt was written to the prompt cache on the first call.
- **No `effort` control** in the native provider (no `output_config` passthrough). Haiku 5.5 runs at its default
  (adaptive thinking, effort `medium`); `max_tokens=1500` bounds output + thinking per call.
- **`BaseLLM` subclasses must pass `model=` in the constructor** (validated before field defaults apply).
- **Pricing:** Sonnet 5.5 cache reads are **$0.20**/MTok (P2's `config/prices.json` has $0.10).

## M1 decisions

7. **World logic lives in `src/compintel/world/`, scripts are thin wrappers.** Tests import the generator, server and
   MCP server directly. `story.py` is the single source of truth; HTML, SQLite, ground truth are rendered from it.
8. **MCP "as of" week comes from the server's environment (`MARKET_AS_OF_WEEK`), not a tool argument.** crewai's
   `MCPServerStdio` has an `env` field, so each weekly run starts the server with its week. The spec's tool signatures
   (`funding_rounds(company)`, `app_rating_trend(company, weeks)`) stay unchanged and future data can't leak.
9. **Fake web disables directory listings** so an agent can only fetch URLs it is given.
10. **Company names checked** (2026-10-30 web search): no notable real company named KiranaCloud, StockSathi, ShelfMitra
    or BahiBook. News outlets are fictional too (Retail Tech Daily, The Mumbai Ledger, Startup Gupshup, ...).
11. **`battlecard_template.pdf` deferred to M4.** No PDF writer is in §19.2, and `PDFKnowledgeSource`'s reader dependency
    has to be checked first; M4 generates the PDF together with the knowledge sources that read it.
12. **Ground truth extended:** besides the spec's `material_signals` / `must_not_claim` / `expected_threat`, each week has
    `must_surface` (subtle signals a routine brief must still mention: week 3's BahiBook PM, Kolkata office, rating
    dip) and `must_discount` (the acquisition rumor). A test asserts every `detail_contains` substring is findable in
    that week's sources.
13. **Changelogs are cumulative with dated entries** (incl. "week 0" history), like real changelog pages; M2's
    ChangelogDiffTool diffs week N against N-1, and week 1 against its own dates.

## M2 decisions

14. **Scrape guard: escape hatch + hook (decided with the developer).** crewai-tools' `ScrapeWebsiteTool` fetches through
    `safe_get`, whose SSRF guard blocks `127.0.0.0/8`. The only switch, `CREWAI_TOOLS_ALLOW_UNSAFE_PATHS=true`, is
    process-wide and also lifts crewai-tools' file-path containment. `config.apply_env()` sets it inside compintel runs
    only; `hooks.url_allowlist` (`PRE_TOOL_CALL`, `tools=["read_website_content"]`) allows exactly `FAKEWEB_URL`'s
    host:port; `make_scout` fails closed if that hook isn't registered. M3 adds a file-path hook for FileReadTool.
15. **`mcpadapt==0.1.20` added (decided with the developer)** in the `mcp-adapter` dependency group, synced by default via
    `[tool.uv] default-groups` (a plain `uv sync` otherwise removes it and MCPServerAdapter shows an install prompt).
16. **`market-signals` console script.** crewai names stdio MCP tools `"<command>_<args>_<tool>"`, capped at 64 chars;
    with `sys.executable` + a script path the names became `home_soumit_..._040fb612` and the LLM couldn't find
    `funding_rounds`. A bare command gives `market_signals_funding_rounds`. `scripts/mcp_market_signals.py` stays as a wrapper.
17. **Hook tool names are sanitized names:** `read_website_content`, not the spec's `scrape_website`.
18. **Our parsing tools take `(company, week)`, not HTML or URLs**: they build the fake-web URL themselves (no LLM-chosen
    URL to police) and return the week-over-week diff computed in code.
19. **Scouts get `max_tokens=4096`** (global default stays 1500). The first live week-2 run hit exactly 1500 output tokens
    on every final answer (Haiku's thinking counts against it), truncating the report: 2/4 material signals.
20. **SignalReport schema in the scout prompt.** With tools, crewai's loop sends no response schema; after the loop,
    `Agent.kickoff` tries `response_format.model_validate_json(answer)` and only on failure calls the LLM Converter.
    Without the schema Haiku answered clean JSON with invented fields (`kind: "pricing"`, `title`), so every scout paid
    an extra conversion call. The schema (from crewai's own `generate_model_description`) removed it; verified live.
21. **Kept `Agent.kickoff_async` × 3 over `kickoff_for_each`** (comparison in the README): 2× faster (kickoff_for_each is
    sequential), and `Agent.copy()` drops `mcps` in crewai 1.15.33, so kickoff_for_each scouts silently lose MCP tools.
    `test_crewai_agent_copy_drops_mcps` pins that behaviour and will fail when upstream fixes it.
22. **`P3_DATA_DIR` setting** (like `P3_RUNS_DIR`) so tests run against a freshly generated world.

## M2 findings

- **Native tool calling with a custom LLM:** return a list of Anthropic `tool_use` blocks (`ToolUseBlock` with `name`,
  `input`) from `BaseLLM.call` and crewai's AgentExecutor runs the tools (`call_llm_native_tools` → `execute_native_tool`).
- **`cache_function(args, result) -> bool`** is called after each tool run with the tool's arguments and result;
  `False` means "don't cache this result".
- **MCP stdio `env` is merged with mcp's safe default environment** (PATH, HOME, ...); API keys are not passed to the
  server subprocess.
- **`HookAborted` at `PRE_TOOL_CALL` blocks only that call**: the tool result becomes "Tool execution blocked by hook.
  Tool: <name>" (the abort reason is not shown to the model) and the agent continues. At `PRE_MODEL_CALL` it ends the run.
- **Live results (Haiku 5.5):** week 2: 4/4 material signals, 10 calls, ~35 s, $0.0086. Week 3: BahiBook GST e-invoicing
  PM, Kolkata office, 3.9 rating surfaced; the acquisition article filed as `rumor`, credibility 0.2. Quality notes for
  M3 to handle: duplicate signals for one fact (pricing page + changelog), unchanged plans reported as `pricing_change`.

## M3 decisions

23. **Three tasks instead of two.** `output_file` writes `pydantic.model_dump_json()` when `output_pydantic` is set,
    so the spec's `write_short_brief` with both `output_pydantic=Brief` and `output_file=…md` would write JSON into
    a .md. `write_short_brief` produces the typed Brief (guardrails, callback); a small `render_draft` task calls
    `RenderBriefTool` (`result_as_answer=True`) and its verbatim markdown goes to `output_file`.
24. **`crew_refs.py` in the crew folder.** crewai's JSON loader imports `{"python": ...}` references only from files
    under the crew file's own folder (`load_crew` fixes the project root there). With our src layout,
    `compintel.models.Brief` is outside it. `crew_refs.py` re-exports the real objects, so identities are kept.
    Tool `"module:Class"` refs are not restricted, so `RenderBriefTool` and `FileReadTool` are referenced directly.
25. **LLMs via `compintel/llms.py`** (PEP 562 `__getattr__` → `config.make_llm`), referenced as
    `crew_refs.analyst` / `crew_refs.writer`. JSON has no env interpolation, and hard-coding `anthropic/...` in
    JSON would bypass config. `llms.scope(fake=...)` must wrap the *load*: `Task` binds a string guardrail to the
    agent's LLM at construction (`LLMGuardrail(llm=self.agent.llm)`), so swapping `agent.llm` later leaves the
    guardrail on the real model.
26. **Credibility rule is a function guardrail** (`low_credibility_only_discounted`), and the string guardrail is
    reworded to what can be judged from the brief alone. See the README for the live evidence. The spec's own
    §12.2 note (string guardrails for checkpoint-safety) is revisited in M5 when checkpointing arrives.
27. **Guardrail order: LLM rule first, function checks last.** crewai retries each guardrail in its own loop and
    re-checks a retry only with that guardrail (`_invoke_guardrail_function`). With functions first, a retry asked
    for by the LLM rule skipped the citation checks (seen live: a truncated, Converter-rebuilt draft got through).
    Trade-off: a function check's retry is not shown to the LLM rule again. Pinned by
    `test_a_retry_is_rechecked_only_by_the_guardrail_that_asked_for_it`.
28. **Citation guardrail checks ids against the run's real inputs**: a crew `before_kickoff_callbacks` entry
    (`register_signals`) records this run's ids and low-credibility ids (module-level; one crew run at a time).
29. **Writer gets the Brief schema in its prompt** (`{brief_schema}` input), as the M2 scouts did: an agent with a
    tool runs crewai's tool loop, which sends no response schema. The tool-less analyst gets it natively.
30. **`LONG_OUTPUT_MAX_TOKENS = 8192`** in config for agents whose answer is a whole document (scouts, writer).
    Live at 1500: empty answers after thinking used the budget ("Invalid response from LLM call - None or empty",
    3 crewai retries, then failure). At 4096 a week-3 Brief was still truncated. Plus brevity rules in the prompt.
31. **Drafts are cwd-relative (`briefs/drafts/`).** crewai rejects inputs that would make `output_file` absolute, so
    the CLI chdirs to the project root (as `crewai run` assumes) and tests run in a tmp cwd.
32. **`file_read_allowlist` hook** (`PRE_TOOL_CALL`, `read_a_files_content`): FileReadTool may only read under
    `briefs/` (paths resolved, so `briefs/../.env` fails). Needed because the M2 escape hatch also lifts
    FileReadTool's own `base_dir` containment. `short_update.load()` fails closed without it.
33. **Market Analyst has no tools in this crew** (spec §7 lists MCP + News): in a routine week the scouts already
    gathered everything, and a tool-less agent gets native structured output in one call.
34. **`markdown=True` is left for the deep dive (M4)**: here the markdown comes from deterministic rendering.
35. **`crew.jsonc` has no `memory`/`embedder` yet**: unified Memory arrives in M7.

## M3 findings

- **Task order of operations** (`Task._execute_core`): agent result → (if guardrails) guardrails, each retrying in
  its own loop → `POST_STEP` hook → task `callback` → crew `task_callback` → `output_file` → `TaskCompletedEvent`.
  Without guardrails, `output_pydantic` conversion happens before; with guardrails it happens after a guardrail
  returns a string. A guardrail that returns a `TaskOutput` is kept as-is (no conversion), which our function
  guardrails use to hand back the parsed Brief.
- **`LLMGuardrail` is a hidden agent**: role "Guardrail Agent", `Agent.kickoff(response_format=LLMGuardrailResult)`,
  on the task agent's LLM. It sees only `task_output.raw` and the rule text.
- **Guardrail events cover both kinds**: `LLMGuardrailCompletedEvent` (despite the name) fires for function guardrails
  too, with `guardrail_type` and `guardrail_name`; `listeners.GuardrailLog` collects them for the CLI.
- **`result_as_answer`**: the agent loop ends right after the tool returns; no further LLM call (1 call for render).
- **Live runs (Haiku 5.5):** week 1: 6 calls, $0.0071 (one LLM-rule retry). Week 3: 5 calls, $0.0061 (the credibility
  function caught the rumor used as fact, retry passed); threat levels match ground truth 3/3; 429 words.
- **Backstory experiment:** +8% input tokens for 193 vs 18 words, identical judgements (README table).

## M4 decisions

36. **Three variants from one @CrewBase class** (`variant=` constructor argument): `sequential`, `manager_llm`,
    `manager_agent` (default). The Head of Strategy is not an `@agent` (crewai rejects a manager inside `agents`).
37. **Hierarchical variants clear every task's `agent`.** With a task agent set, crewai's `_update_manager_tools` lets
    the manager delegate only to that agent; the spec's "manager may reassign" needs the agent unset.
    `intended_agents` keeps the YAML intent for the comparison.
38. **No `async_execution` in the hierarchical variants.** Every hierarchical task runs on the manager's one executor;
    concurrent use raised `RuntimeError: Executor is already running. Cannot invoke the same executor instance
    concurrently` (crewai 1.15.33), and with knowledge sources on, the same collision ended the process silently
    (exit 0, no exception). Parallel analyses only in `sequential`.
39. **The brief's LLM rule is an explicit `LLMGuardrail(description=..., llm=...)`**: crewai refuses a plain-string
    guardrail on a task without an agent ("Agent is required to use non-programmatic guardrails"). Judge on Haiku
    even with `--final`.
40. **No `from __future__ import annotations` in guardrails.py**: a single `guardrail=` is validated with
    `inspect.signature` without evaluating strings, so `-> tuple[bool, Any]` must be a real type.
41. **Render in `@after_kickoff`, not a `result_as_answer` task** as in M3: in hierarchical runs every task goes
    through the manager. `markdown=True` is used on the escalation memo (prose), not on the JSON brief.
42. **Schemas in task descriptions** (`{threats_schema}`, `{synthesis_schema}`, `{brief_schema}`): agents with tools and
    managers with delegation tools get no response schema from crewai's loop (M2/M3 finding).
43. **All deep-dive agents on `LONG_OUTPUT_MAX_TOKENS`**: every answer is a whole analysis or document.
44. **Market Analyst: `news_search` only, no MCP** (spec lists MCP + News): the scouts already pulled market data into
    the signals, and each `mcps` server is a subprocess per agent.
45. **Knowledge**: `TextFileKnowledgeSource` (3 md) + `PDFKnowledgeSource` (battlecard) at crew level; one doc each at
    agent level for the Pricing and Product Analysts; absolute `Path`s from `P3_KNOWLEDGE_DIR` (a string path is
    resolved against `./knowledge/`); `KnowledgeConfig(results_limit=3)` with crewai's default 0.6 threshold (real
    MiniLM scores measured 0.67-0.80). Embedder from `config.embedder_config()` on the crew and every agent.
46. **Battlecard PDF written by hand** (`generate.minimal_pdf`, ~30 lines, Helvetica, no new dependency), read back by
    `pdfplumber` (crewai's PDF reader) in a test.
47. **Test embedder**: `FakeEmbedder` subclasses both crewai's `CustomEmbeddingFunction` and chromadb's
    `EmbeddingFunction` (two validators check different bases), injected via `config.embedder_scope()` in conftest.
48. **Test storage isolation**: crewai computes chroma's storage path once at import (`DEFAULT_STORAGE_PATH`) from
    `CREWAI_STORAGE_DIR` (an app *name* under `XDG_DATA_HOME`) or the cwd's name. conftest now sets both at import.
    Before that fix, tests and smoke builds wrote fake-embedder collections into the real
    `~/.local/share/p3_crewai_competitive+intelligence`, and the next live run failed with chroma's "An embedding
    function already exists in the collection". The polluted chroma files (all created by those runs that day) were
    deleted; `crewai reset-memories` needs a crew/flow entry point, which arrives in M5. A test pins the isolation.
49. **`validate_deep_dive_inputs` crew-scoped hook** (§12.1) is left for M8 with the rest of the hook set;
    `checkpoint=CheckpointConfig(...)` (§8.3) for M5; `memory=` for M7.

50. **Haiku 5.5 registered for native structured outputs** (`compintel/compat.py`, decided with the developer after
    the developer's own sequential run printed the errors). crewai 1.15.33 lists `claude-sonnet-5` but not
    `claude-haiku-5` in `NATIVE_STRUCTURED_OUTPUT_MODELS`, so Haiku got the forced-`structured_output`-tool
    fallback. On crewai's nested planning models it failed intermittently: `StepObservation` fields came back wrapped
    in an extra key (`observation`, `input`, `parameters`, ...), `ThreatAssessments.threats` as a JSON string. A live
    probe showed Haiku 5.5 accepts the native path. The shim adds the prefix inside `make_llm`;
    `test_upstream_still_lacks_haiku_5` fails when crewai adds it, so the shim can be deleted.
51. **Agent-planning loops bounded**: `max_replans=1`, `max_step_iterations=3` on both `PlanningConfig`s (spec sets only
    effort/attempts/steps; defaults are 3 replans and 15 iterations per step, and the agent's `max_iter` doesn't
    apply to plan steps). With observations working after #50, the Strategist made 144 calls in 9 minutes, still
    running, before a timeout stopped it ($0.17).
52. **The first M4 comparison was confounded, and the errors were hidden by me**: `process_comparison.py` was run
    with stderr sent to /dev/null. Every Strategist step observation had failed, and crewai's fallback (despite the
    log text "Defaulting to conservative replan") returns "step succeeded, no replan", which kept the loop short.
    `listeners.FailureLog` now counts `LLMCallFailedEvent` and `StepObservationFailedEvent`; the CLI and the
    comparison report them, and comparison runs keep stderr in a log.

53. **Hierarchical variants also clear `task.tools`.** A `Task` built with an agent copies that agent's tools into
    `task.tools` (crewai `task.py`, "if not self.tools and self.agent and self.agent.tools"), and in a hierarchical
    crew the manager is handed the task's tools next to its delegation tools. Clearing only `task.agent` left the
    specialists' tools on the tasks, so in every hierarchical run before this fix the manager called
    `pricing_parse` / `changelog_diff` / `news_search` itself and never delegated (crewai's own `task.delegations`
    was 0; a tally of tool events showed the manager's tool calls). The earlier conclusion "Haiku managers don't
    delegate" was wrong: they had the work tools in hand. Tests now require that a manager is offered exactly
    the two delegation tools (verified to fail without the fix).

## M4 findings

- **Hierarchical execution**: `_get_agent_to_use` returns the manager for every task; delegation tools are injected
  per task (`_update_manager_tools`); a custom manager must have no tools of its own (crewai raises).
- **`ConditionalTask`**: may not be first or async; its condition receives the previous task's `TaskOutput`; a skipped
  task leaves an empty output. Condition is a module-level function (checkpoint-safe, spec §2.2).
- **Crew planning**: one "Task Execution Planner" call returns `PlannerTaskPydanticOutput` (one plan per task number),
  appended to each task description. Live it was the single biggest call (~28k in / 8k out, $0.004-0.007).
- **Agent planning (`PlanningConfig`)**: with `llm=None` it falls back to the agent's own LLM (not OpenAI). It turns
  one task into a plan/execute/observe loop; bound it with `max_replans` / `max_step_iterations` (#51).
- **Knowledge retrieval costs an LLM call per task**: "rewrite the user query" turns the task prompt into a search query.
- **Live comparison** (after #50-#53): same quality and cost across the three variants; managers delegate the tool-backed
  analyses and do the rest themselves; `sequential` fastest. See docs/process-comparison.md.

## M5 decisions

54. **Flow class and CLI entry points both live in `flow.py`; no `main.py`.** `crewai run --inputs` imports the
    `kickoff` script's module and calls the one Flow class *defined in it* directly, and a Flow subclass loses its
    inherited `@start`/`@listen` graph in crewai 1.15.33 (the metaclass registers only methods in the class body;
    a `WeeklyBriefFlow(CompetitiveIntelFlow)` had zero start methods and plotted empty). So the spec's flow.py +
    main.py split can't work; pyproject's `kickoff` / `plot` / `replay` point at `compintel.flow` / `compintel.replay`.
55. **`CompetitiveIntelFlow.kickoff` is overridden** (a plain method, so the graph is unaffected): it bootstraps
    (hooks, cost listener: scouts and crews fail closed without them) and, for weeks after the first, defaults
    `restore_from_state_id` to last week's published state from `runs/state_index.json`. Every way of running a
    week (CLI, `crewai run`, `crewai run --inputs`) then forks correctly.
56. **The router is code, with history.** `materiality.assess`: a competitor rated "high" last week is continuing,
    not escalating; material = a fresh decisive move (pricing change, funding) or 3+ fresh high-impact signals;
    exactly 2 = a tie broken by one Haiku call (`TieBreak`). On the live scout reports, week 3 routes routine only
    with week 2's threat levels (it has four "new" high-impact signals: scouts have no memory); without them,
    material. That is the spec's case for `@persist`.
57. **`load_week` refuses a fork that didn't hydrate.** crewai falls back silently to a fresh state when
    `restore_from_state_id` isn't found.
58. **Crash-resume = fork the crashed run's persisted state + idempotent steps** (decided with the developer).
    `load_week` records the run as in progress; `scout_all` and the crew steps skip work already in the state;
    `run_week(week, resume=True)` / `compintel run --resume` forks the in-progress state. Not `inputs.id`
    (deprecated) and not Flow checkpointing: resuming our flow from a checkpoint skipped the completed methods and
    then stopped at the `and_` router, so the crew, render and publish never ran.
59. **Crew checkpointing on the ShortUpdateCrew only** (decided with the developer). crewai 1.15.33 can't serialize a
    snapshot while an agent executor is mid-run, and the DeepDiveCrew always has one (parallel analyses in
    `sequential`, the manager through every task otherwise); `test_deep_dive_checkpoints_still_fail_upstream`
    pins it. The ShortUpdateCrew (one task at a time, no manager) writes one checkpoint per task;
    `short_update.resume()` continues a killed run and the finished tasks aren't re-run.
60. **A restore drops what it can't serialize, so `resume()` re-attaches it**: callable guardrails, our LLM objects
    (from a freshly loaded crew), the signal registry (from `crew.checkpoint_inputs`); `save_draft` /
    `register_signals` callbacks need `CREWAI_DESERIALIZE_CALLBACKS=1`, set by `resume()` for our own snapshot files.
61. **`@tool` tools can't be checkpointed**: `Tool.func` is a plain `Callable` field with no serializer, so every
    snapshot of an agent holding one fails. Scouts keep `pricing_parse` / `changelog_diff` as `@tool`; checkpointable
    crews get `PricingParseTool` / `ChangelogDiffTool` (`BaseTool` twins over the same functions).
62. **`replay` script** for `crewai replay -t <task_id>` (the CLI runs `uv run replay <id>`): rebuilds the crew the
    stored task outputs came from (3 tasks: short update; 6: deep dive, variant from the stored agents) and
    re-registers the signals (`Crew.replay` doesn't run before-kickoff callbacks).
63. **Test harness**: conftest resets crewai's lazily-registered checkpoint handlers (`_handlers_registered`) and our
    `runtime._done` per test, because the per-test event-bus scope removes handlers registered inside it.
64. **Plot**: `Flow.plot()` always renders into a fresh temp dir; `compintel plot` copies the html + css + js to `docs/`.
65. **`SqliteProvider` doesn't create its folder**; `checkpoint_db()` does.

## M5 findings

- **Flow methods**: sync methods run in a worker thread (`asyncio.to_thread`), so a crew's plain `kickoff()` works
  there; `scout_all` is async and awaits the scouts. `@persist()` must be called (`@persist` alone fails).
- **`flow.usage_metrics`** = every LLM call in the run; live week 1 it matched the cost ledger exactly (17 requests).
- **Flow checkpoints** are written per method (`method_execution_finished`), but resuming from one doesn't continue
  past an `and_` router (see #58).
- **Crew checkpoint serialization failures are silent**: the checkpoint listener catches them and emits
  `CheckpointFailedEvent`; nothing is logged. Our own fakes also broke it (`ScriptedLLM.calls`, the fake embedder
  class), fixed for tests.
- **Silent process exits in scratch probes**: several standalone probe scripts died with exit status 1 and no
  output while chromadb's Rust layer was upserting knowledge at crew kickoff. Never seen in the test suite or in
  live runs; not resolved.
- **Hierarchical guardrail retries go to the manager.** Live week 2 (manager_agent): the Head of Strategy wrote the
  brief itself and rewrote it on each guardrail retry, keeping a hedge ("reported but not yet confirmed") on a
  confirmed launch; all three attempts failed the LLM rule and the week stopped. Resumed with the sequential
  variant (see the README's M5 section).
- **An empty tool result can stall a run.** Live week 3: BahiBook has no week-3 news, `news_search` returned
  `{"articles": []}`, the scout repeated the call, and crewai's loop detector raised after 5 identical calls; the
  exception failed `asyncio.gather` and the whole week. The tool now answers in words ("No news articles ... don't
  search again"). One failed scout still fails the week (loud, resumable), by choice.
- **Short-update threat levels now go through `threat_levels_consistent`** too: live week 1 rated StockSathi "high"
  over an unchanged Basic plan (no high-impact signal). That brief stayed published, so week 2's router counted two
  StockSathi signals as continuations; week 2 still routed material on ShelfMitra's fresh moves.
- **Live three-week run**: routes routine → material → routine, as in the ground truth; week 3 threat levels 2/3
  (BahiBook low, expected medium). `flow.usage_metrics.successful_requests` equalled the cost ledger's call count in
  every run.

## M6 decisions

66. **The review is `@human_feedback(emit=["approve","revise","reject"], llm=REVIEW_LLM, default_outcome="revise",
    learn=True, metadata={"stage": "editorial"})`** on `editorial_review`, which listens to
    `or_(render_draft, "revised")`: the first draft and every revision go through the same step.
    `default_outcome="revise"` as in the spec: empty feedback is not approval.
67. **`editorial_review` returns the `Brief`, not its markdown** (spec deviation). With `learn=True` crewai
    pre-reviews a Pydantic output *as that model* (`llm.call(response_model=Brief)`), so the published .md and
    .json stay one document. Providers render it for the human (`ConsoleReview` swaps in the markdown).
68. **`REVIEW_LLM` is an explicit Haiku instance with an 8192-token budget**, built at import: the decorator needs the
    object at class definition, and the pre-review rewrites a whole Brief. Left unset, crewai would use
    `create_llm(None)` ($MODEL, else OpenAI). Its config (model + max_tokens) is serialized into a paused review,
    so a resume in another process uses the same model.
69. **The revise loop is bounded by a router on the feedback label**: `@router("revise") revise_brief ->
    Literal["revised", "revision_limit"]`. After `MAX_REVISIONS = 2` revisions, a third "revise" routes to publish
    with an editor's note quoting the unapplied request. `RevisionCrew` (one Editor, plain-Python crew,
    `every_claim_cites_signal` guardrail) applies the feedback plus recalled HITL lessons.
70. **Publish what the human saw**: `publish` takes the Brief from `last_human_feedback.output`, which can differ
    from what `editorial_review` returned (the pre-review). After an async resume that output is a dict;
    `review.as_brief()` handles both.
71. **Quiet weeks skip review** (spec diagram: quiet_week → publish). **Reject** archives to `briefs/rejected/`,
    clears `in_progress` and publishes nothing, so next week can't fork it: rerun the week.
72. **Audit log in the state** (`CIState.feedback_log`, written to `briefs/week-NN.feedback.json`), not crewai's
    `human_feedback_history`: that is a Flow field, not state, so a review resumed in a new process starts it empty.
    Each entry records `changed_by_lessons` (the pre-review changed the draft).
73. **Two providers, chosen per run via `flow_config.hitl_provider`**: `ConsoleReview` (crewai's `ConsoleProvider`,
    shown markdown) and `PendingReview` (writes `runs/pending_reviews/<flow_id>.json`, raises
    `HumanFeedbackPending`). `compintel pending` / `compintel review <id> "text"` = `Flow.from_pending(id)
    .resume(text)`. The decorator has no `provider=`, so `crewai run` and tests choose without code changes.
74. **The Flow gets our `Memory`** (`compintel/memory.py`: Haiku, local ONNX embedder, LanceDB under
    `runs/memory/`) via an `__init__` default. Lessons need to outlive the process. M7 does memory properly.
75. **Crew-level HITL** (`human_input=True`) is a flag on RevisionCrew's task, used by `compintel revise
    --human-input` only; **hook-level HITL** is `hooks.render_gate` (`ctx.request_human_input` at PRE_TOOL_CALL on
    `render_brief`), opt-in via `compintel brief --render-gate`.
76. **Scouts use the BaseTool twins** (`PricingParseTool`, `ChangelogDiffTool`), not the `@tool` versions (see
    findings). The `@tool` versions stay as the decorator demo (tests).
77. **Tests**: `tests/hitl.py` has a scripted reviewer (a real `HumanFeedbackProvider`) and one scripted model
    for every call the review makes, matched on crewai's prompt templates and response models (outcome, lessons,
    pre-review, Memory's analysis). It patches `_resolve_llm_instance` in crewai's runtime and human_feedback
    modules (`importlib`: `crewai.flow` re-exports the decorator under the module's name). Cross-process resume
    runs `python -m tests.resume_review` in a subprocess.

## M6 findings

- **Every Flow builds itself a `Memory()` when it has none**, with crewai's OpenAI defaults (gpt-5.4-mini, OpenAI
  embeddings). Harmless while unused; `learn=True` uses it, so it's now always ours.
- **`memory.recall(source=...)` does not filter by source** in 1.15.33: it only decides which *private* records
  are visible. The pre-review therefore applies every memory it recalls, not only HITL lessons (fine while lessons
  are all there is; M7 must scope them). `hitl_lessons()` filters by `record.source` itself.
- **The pre-review always recalls with `depth="deep"`**: an LLM query-analysis call per review, even with an empty
  memory, before any lesson exists.
- **`kickoff()` returns `HumanFeedbackPending`** (it doesn't raise). crewai persists state + context in the default
  SQLite (`flow_states.db`, table `pending_feedback`) and clears it when a resume has recorded the answer, before
  running the listeners. So a resume that fails in a later step (e.g. the revision crew) is no longer pending.
- **After an async resume the reviewed output is a dict** (restored from JSON), as documented. Confirmed live.
- **Inside a Flow, a crew checkpoint serializes every entity of the run**, not only the crew: the Flow and every
  standalone agent (the scouts). The scouts held `@tool` functions, so *every* ShortUpdateCrew checkpoint in a
  flow run failed, logged only as a warning (`Auto-checkpoint failed`). This was already true in M5's live
  weeks (the checkpoint tests run the crew alone). Fixed by giving scouts the BaseTool twins; live week 3 then
  wrote 3 checkpoints, no warning.
- **A refused `result_as_answer` tool doesn't stop a crew**: `HookAborted` at PRE_TOOL_CALL blocks that call, and
  since `render_brief` is the task's answer, "Tool execution blocked by hook. Tool: render_brief" *becomes* the
  task output (and its output file). `hooks.render_refused()` detects it.
- **A router can listen to a `@human_feedback` outcome, and `or_` can mix a method with a label**, so the revise
  loop needs no hand-rolled state machine. Methods re-run in the loop (crewai clears the resume flag first).
- **Live learning loop** (async provider, each answer in a new process): week 1 feedback "Always lead with the
  single biggest threat to Kolkata, and keep the headline under 15 words" → `revise` → two lessons stored
  (`source=hitl`, scope `/`, LLM-inferred): "Lead with the single biggest threat to the target market (e.g.,
  Kolkata) in the headline and summary." and "Keep headlines under 15 words." Week 2's crew headline (19
  words) reached the reviewer as 15 (`changed_by_lessons: true`); week 3's, 22 → 11. Routes routine → material →
  routine again; threat levels vs ground truth 1/3, 3/3, 2/3.
- **One unexplained lost output**: live week 2 (`run --review async`, stdout to a file) paused and saved
  correctly, but its last minute of output (the summary lines) never reached the log. Not reproduced ($0 probe
  of a deep dive + memory exit: clean, exit 0). Runs since then use `PYTHONUNBUFFERED=1` and record exit codes.

## M7 decisions

78. **One shared `Memory` per process** (`memory.shared()`: Haiku, local ONNX embedder, LanceDB `runs/memory`,
    `recency_half_life_days=14`) and views over it. The module is `memory.py` (spec: `memory_setup.py`).
79. **Scopes**: `/competitor/<slug>`, `/brief/week-NN`, `/hitl`. No `/company/kiranacloud`: our own docs are
    knowledge (M4), not memory.
80. **Scouts read, the Flow writes** (spec: scouts write their own scope). Scouts get a read-only
    `/competitor/<company>` scope (`ReadOnlyScope`): crewai recalls into the prompt before the kickoff, gives the
    agent only the recall tool, and the kickoff save finds nothing. `publish` records each signal as
    `"Week N: <detail>"` with `signal_id`, `source_ref`, `kind`, and importance from `impact_on_us`. Why: an agent's
    own save runs mid-week and broke every later crew checkpoint (see findings); the Flow's facts are
    structured (week, id, source) instead of LLM-extracted, and cheaper: the first design's week 1 made 49 calls
    (M6's week 1: 18); with this one, week 3 made 27 (M6's week 3: 21).
81. **The Flow's own memory is the `/hitl` scope**, so `learn=True` lessons land there and the pre-review reads
    only lessons. With an unscoped memory it would have "applied" brief facts as lessons (M6 finding).
    `publish` uses Flow memory's `extract_memories` for the brief (one call) and saves into `/brief/week-NN`
    with scope, categories and importance given, so crewai skips its per-fact analysis call.
82. **Brief Writers** (ShortUpdateCrew via `crew.jsonc` → `crew_refs.writer_memory`; DeepDiveCrew in Python) get a
    read-only `MemorySlice` over `/competitor`, `/brief`, `/hitl`. No crew-level `memory=`: it would save every
    task's output (an extraction plus an analysis call per fact) into LLM-chosen scopes, and duplicate what
    publish stores. The checkpoint resume re-attaches the slice (restored views come back unbound).
83. **Agents' recall is always `depth="shallow"`** (`ShallowScope` / `ShallowSlice`, a one-line `recall`
    override): crewai has no setting for the depth an agent recalls with, and deep recall cost 23 s and 4
    calls per task-length query live, and broke checkpoints. The chat also recalls shallow.
84. **Ask the Analyst** (`chat.py`): `@ConversationConfig(llm, router=RouterConfig(llm), trace_scope="turn")`,
    explicit models (left unset the router has none), class-level `@persist()` for sessions, the analyst slice as
    its memory (not the OpenAI-default one). Routes are `@listen` handlers; the docstrings are the router's
    catalog. Retrieval is code, then one call (spec: an agent with recall + FileReadTool): 2 calls per turn.
85. **The Market Analyst gets the long-output budget** too (8192): its ThreatAssessments JSON was truncated at 1500.
86. **`runtime.restore_std_streams()`** after every CLI run (see findings: stdout race).
87. **Tests**: conftest scripts Memory's model (`fakes.memory_reply`: analysis, query analysis, consolidation,
    extraction), clears the shared store per test, and resets crewai's runtime state per test.

## M7 findings

- **A read-only `MemorySlice` ignores writes silently** (`remember` returns None). The spec's "write fails" is a
  no-op; tests assert nothing was stored.
- **Slices take scope prefixes, not globs**: `/competitor` covers every company (`/competitor/*` isn't syntax).
- **Deep recall runs `RecallFlow`, an agent's own memory save runs `EncodingFlow`; both are Flows that join the
  run's state** with a field crew checkpoints can't serialize (`initial_state`, a class). After the first one,
  every crew checkpoint in the run failed (only a logged warning). Live M7 week 1 (first design: scouts
  saved their own memories): 3 of 3 failed. With read-only shallow views: none (regression test runs scouts and
  the crew in one Flow).
- **crewai keeps the run's entities in a context variable, and a run that starts with one already set reuses
  it**: a memory save made outside any run (e.g. in a test, before kickoff) leaks its EncodingFlow into the next
  run. Tests reset it (`crewai_event_bus.reset_runtime_state()`).
- **Agent memory**: an agent with `memory=` gets "Search memory" (+ "Save to memory" unless read-only), a recall
  before every task/kickoff (default deep), and a save after (`extract_memories` = an LLM call, even when the
  memory is read-only, then `remember_many`). Crew task saves skip read-only memories.
- **Deep recall only calls the LLM for queries of 200+ characters** (`query_analysis_threshold`) or low
  confidence; an agent's recall query is its whole task prompt, so always.
- **Consolidation works as documented**: a near-identical save (≥ 0.85 similarity) costs one call that decides
  keep / update / delete; live, the repeated pricing fact stayed one record.
- **Private records**: visible only to recall with the same `source` (or `include_private=True`); confirmed live.
- **The lost output of deep-dive weeks (M6, and live M7 week 2) is a stdout race**: crewai's chromadb client
  silences queries with `contextlib.redirect_stdout(io.StringIO())`, which swaps the process-wide `sys.stdout`;
  the deep dive's parallel analysts query knowledge concurrently, two redirects interleave, and the last exit
  restores the other's StringIO. Everything printed afterwards vanished, exit code 0. Reproduced with two
  threads (test in test_cli.py). Probably also the M5 "silent probe exits".
- **The conversational Flow's router sends a JSON context** (`current_user_message`, history, route catalog)
  and gets `{"intent": ...}` as structured output; an unknown intent falls back to `converse`.
- **Rich markup**: an answer citing `[/competitor/shelfmitra]` crashed the CLI print (read as a closing tag).
  Answers print with `markup=False`; competitor facts cite their signal id.
- **Live weeks 1-3**: routes as the ground truth; threat levels 1/3, 3/3, 3/3 (week 3 after my review moved
  BahiBook high → medium). Week 3's since-last-week compares against week 2's threat levels.
- **Test suite: 162 tests, 64-68 s**, over the spec's 60 s target. The slowest: the cross-process review (7.5 s,
  a subprocess) and the scout fan-out (5.5 s).

## Open questions for the developer

- **Coding-agent skills:** `npx skills add crewaiinc/skills` (from docs `/en/skills`). Not installed; optional.
