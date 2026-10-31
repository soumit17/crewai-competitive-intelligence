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

## Open questions for the developer

- **Coding-agent skills:** `npx skills add crewaiinc/skills` (from docs `/en/skills`). Not installed; optional.
