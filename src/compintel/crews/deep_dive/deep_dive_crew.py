"""DeepDiveCrew (spec §8.3): the material-week crew. Classic @CrewBase + config/agents.yaml + tasks.yaml.

Three ways to run the same agents and tasks, for the M4 process comparison (docs/process-comparison.md):

- "sequential":    each task runs once on its YAML agent. No manager.
- "manager_llm":   hierarchical; crewai builds a generic manager from `manager_llm`.
- "manager_agent": hierarchical; our Head of Strategy is the manager (`manager_agent`). The default.

In both hierarchical variants every task runs *through the manager*, which delegates with crewai's "Delegate
work to coworker" / "Ask question to coworker" tools. Task agents are cleared there: with a task agent set,
crewai only lets the manager delegate to that one agent, so there would be no choice to make. Async is also
switched off there (see crew()): the analyses run in parallel only in "sequential".
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from crewai import Agent, Crew, CrewOutput, PlanningConfig, Process, Task
from crewai.project import CrewBase, after_kickoff, agent, before_kickoff, crew, task
from crewai.tasks.conditional_task import ConditionalTask
from crewai.tasks.llm_guardrail import LLMGuardrail
from crewai.utilities.pydantic_schema_utils import generate_model_description
from crewai_tools import FileReadTool

from compintel import knowledge
from compintel.config import AGENT_LIMITS, LONG_OUTPUT_MAX_TOKENS, embedder_config, get_settings, make_llm
from compintel.guardrails import (
    NO_RUMOR_AS_FACT,
    any_high_threat,
    brief_from,
    brief_length_ok,
    every_claim_cites_signal,
    low_credibility_only_discounted,
    register_signals,
    threat_levels_consistent,
)
from compintel.models import Brief, Synthesis, ThreatAssessments
from compintel.tools.changelog_diff import changelog_diff
from compintel.tools.news_search import NewsSearchTool
from compintel.tools.pricing_parse import pricing_parse
from compintel.tools.render_brief import draft_path, render_markdown

VARIANTS = ("sequential", "manager_llm", "manager_agent")


def _schema(model: type) -> str:
    # Agents with tools (and a manager with delegation tools) get no response schema from crewai's tool loop,
    # so structured tasks carry the schema in their description (same fix as M2/M3).
    return json.dumps(generate_model_description(model)["json_schema"]["schema"])


@CrewBase
class DeepDiveCrew:
    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    agents: list[Agent]
    tasks: list[Task]

    def __init__(
        self, variant: str = "manager_agent", final: bool = False, fake: Any = None, verbose: bool = False
    ):
        if variant not in VARIANTS:
            raise ValueError(f"variant must be one of {VARIANTS}")
        self.variant, self.final, self.fake, self.verbose = variant, final, fake, verbose

    def _llm(self, writer: bool = False) -> Any:
        # Every answer here is a whole analysis or document: the long-output budget (config.py).
        return self.fake or make_llm(final=self.final and writer, max_tokens=LONG_OUTPUT_MAX_TOKENS)

    def _agent(self, name: str, **kwargs: Any) -> Agent:
        return Agent(
            config=self.agents_config[name],  # type: ignore[index]
            llm=kwargs.pop("llm", None) or self._llm(),
            allow_delegation=False,
            verbose=self.verbose,
            embedder=embedder_config(),  # local embedder for agent knowledge (not crewai's OpenAI default)
            knowledge_config=knowledge.QUERY_CONFIG,
            **AGENT_LIMITS,
            **kwargs,
        )

    # ---------------------------------------------------------------- agents (collected into self.agents)
    @agent
    def pricing_analyst(self) -> Agent:
        # Agent-level knowledge: only our pricing doc, so retrieval stays on-topic for this specialist.
        return self._agent(
            "pricing_analyst",
            tools=[pricing_parse],
            knowledge_sources=[knowledge.text_source(knowledge.PRICING)],
        )

    @agent
    def product_analyst(self) -> Agent:
        return self._agent(
            "product_analyst",
            tools=[changelog_diff],
            knowledge_sources=[knowledge.text_source(knowledge.ROADMAP)],
        )

    @agent
    def market_analyst(self) -> Agent:
        # Agent planning, cheapest setting (spec §7): one plan, no LLM review after each step.
        return self._agent(
            "market_analyst",
            tools=[NewsSearchTool()],
            planning_config=PlanningConfig(reasoning_effort="low", max_attempts=1, llm=self._llm()),
        )

    @agent
    def strategist(self) -> Agent:
        # Agent planning with review: plan, execute, observe, replan; bounded by attempts and steps (spec §7).
        return self._agent(
            "strategist",
            planning_config=PlanningConfig(
                reasoning_effort="medium", max_attempts=2, max_steps=6, llm=self._llm()
            ),
        )

    @agent
    def brief_writer(self) -> Agent:
        # FileReadTool reads last week's brief; hooks.file_read_allowlist keeps it under briefs/.
        return self._agent("brief_writer", tools=[FileReadTool()], llm=self._llm(writer=True))

    def head_of_strategy(self) -> Agent:
        """The custom manager. Deliberately not an @agent: crewai rejects a manager inside `agents`, and a
        manager may not have tools (crewai gives it the delegation tools)."""
        return Agent(
            config=self.agents_config["head_of_strategy"],  # type: ignore[index]
            llm=self._llm(),
            allow_delegation=True,
            verbose=self.verbose,
            **AGENT_LIMITS,
        )

    # ---------------------------------------------------------------- tasks (collected in this order)
    @task
    def pricing_analysis(self) -> Task:
        return Task(config=self.tasks_config["pricing_analysis"])  # type: ignore[index]

    @task
    def product_overlap(self) -> Task:
        return Task(config=self.tasks_config["product_overlap"])  # type: ignore[index]

    @task
    def market_threats(self) -> Task:
        return Task(
            config=self.tasks_config["market_threats"],  # type: ignore[index]
            output_pydantic=ThreatAssessments,
            guardrail=threat_levels_consistent,  # function: checks levels against the registered signals
            guardrail_max_retries=1,
        )

    @task
    def synthesis(self) -> Task:
        return Task(config=self.tasks_config["synthesis"], output_pydantic=Synthesis)  # type: ignore[index]

    @task
    def escalation_memo(self) -> ConditionalTask:
        # Runs only if the synthesis (the previous task's output) rates any competitor "high".
        # markdown=True: crewai asks for markdown formatting, which suits a memo (the Brief is JSON).
        return ConditionalTask(
            config=self.tasks_config["escalation_memo"],  # type: ignore[index]
            condition=any_high_threat,
            markdown=True,
        )

    @task
    def write_brief(self) -> Task:
        return Task(
            config=self.tasks_config["write_brief"],  # type: ignore[index]
            output_pydantic=Brief,
            # LLM rule first, deterministic checks last: each guardrail re-checks only its own retries (M3).
            # The rule is an explicit LLMGuardrail, not a plain string: crewai turns a string into an LLM
            # call on the *task agent's* model and rejects it on an agentless task, which the hierarchical
            # variants have. The judge stays on Haiku even with --final: a pass/fail check doesn't need
            # Sonnet.
            guardrails=[
                LLMGuardrail(description=NO_RUMOR_AS_FACT, llm=self._llm()),
                every_claim_cites_signal,
                low_credibility_only_discounted,
                brief_length_ok,
            ],
            guardrail_max_retries=2,
        )

    # ---------------------------------------------------------------- kickoff hooks
    @before_kickoff
    def prepare_inputs(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Normalise inputs, add the schemas and the previous brief, register signals for the guardrails."""
        week = int(inputs["week"])
        previous = Path("briefs") / f"week-{week - 1:02d}.md"
        inputs.setdefault("week_nn", f"{week:02d}")
        inputs.setdefault("previous_brief", str(previous) if previous.exists() else "none")
        inputs.setdefault("threats_schema", _schema(ThreatAssessments))
        inputs.setdefault("synthesis_schema", _schema(Synthesis))
        inputs.setdefault("brief_schema", _schema(Brief))
        self._started = time.perf_counter()
        self._week = week
        return register_signals(inputs)

    @after_kickoff
    def save_outputs(self, output: CrewOutput) -> CrewOutput:
        """Save the draft (JSON + rendered markdown) and this run's crewai token usage."""
        write = self._task_by_name("write_brief")
        if write.output is not None:
            brief = brief_from(write.output)
            path = draft_path(brief.week)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(brief.model_dump_json(indent=2))
            # Rendered in code here, not by a result_as_answer tool task as in M3: in the hierarchical
            # variants every task goes through the manager, so a tool-only task would depend on its
            # delegation.
            draft_path(brief.week, ".md").write_text(render_markdown(brief))
        usage_log = get_settings().p3_runs_dir / "crew_usage.jsonl"
        usage_log.parent.mkdir(parents=True, exist_ok=True)
        with usage_log.open("a") as f:
            row = {
                "crew": "deep_dive",
                "variant": self.variant,
                "week": self._week,
                "seconds": round(time.perf_counter() - self._started, 1),
                "token_usage": output.token_usage.model_dump() if output.token_usage else None,
            }
            f.write(json.dumps(row) + "\n")
        return output

    def _task_by_name(self, name: str) -> Task:
        return next(t for t in self.tasks if t.name == name)

    # ---------------------------------------------------------------- the crew
    @crew
    def crew(self) -> Crew:
        manager: dict[str, Any] = {}
        if self.variant != "sequential":
            # Remember who the YAML intended, then let the manager choose (see module docstring).
            self.intended_agents = {t.name: t.agent.role for t in self.tasks if t.agent}
            for t in self.tasks:
                t.agent = None
                # crewai 1.15.33: every hierarchical task runs on the one manager agent, whose single
                # executor refuses concurrent use ("Executor is already running"); with knowledge on, the
                # same collision ended the process silently. So the three analyses only run in parallel in
                # "sequential".
                t.async_execution = False
            if self.variant == "manager_llm":
                manager["manager_llm"] = self._llm()
            else:
                manager["manager_agent"] = self.head_of_strategy()
        return Crew(
            agents=self.agents,  # type: ignore[arg-type]  # list is invariant: list[Agent] vs list[BaseAgent]
            tasks=self.tasks,
            process=Process.sequential if self.variant == "sequential" else Process.hierarchical,
            # Crew planning: before kickoff, a planner writes a step plan into every task description.
            # planning_llm explicitly: crewai's default planner model is gpt-5.4-mini.
            planning=True,
            planning_llm=self._llm(),
            # Crew-level knowledge: all of KiranaCloud's docs, for every agent (and the manager).
            knowledge_sources=knowledge.crew_sources(),
            embedder=embedder_config(),
            verbose=self.verbose,
            **manager,
        )
