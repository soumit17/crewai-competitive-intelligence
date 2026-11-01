"""RevisionCrew (spec §8.4): one Editor, one task, sequential. Applies a reviewer's feedback to the Brief.

Defined in plain Python, the third way to define a crew here (after JSON-first ShortUpdateCrew and
@CrewBase + YAML DeepDiveCrew): one agent and one task don't need config files.

`human_input=True` is crewai's classic crew-level HITL (spec §10.3): after the Editor's answer, crewai asks on
the console for more feedback and the Editor goes again until you press Enter. The Flow doesn't use it (its
@human_feedback step is the review); `compintel revise --human-input` shows it for comparison.
"""

from __future__ import annotations

from crewai import Agent, Crew, CrewOutput, Process, Task

from compintel.config import AGENT_LIMITS, LONG_OUTPUT_MAX_TOKENS, make_llm
from compintel.guardrails import brief_from, every_claim_cites_signal, register_signals
from compintel.models import Brief

REVISE = """You are revising week {week}'s competitive-intelligence brief for KiranaCloud.

The current brief (JSON):
{brief}

The reviewer's feedback:
{feedback}

Lessons from earlier reviews (apply them too):
{lessons}

This week's signals (cite only these ids, or ids from earlier weeks):
{signals}

Apply the feedback. Change only what it asks for (and what the lessons require); keep every citation, every
signal id and the route as they are unless the feedback says otherwise. Return the whole revised Brief."""


def build(human_input: bool = False, fake=None) -> Crew:
    editor = Agent(
        role="Editor",
        goal="Apply the reviewer's feedback to the brief exactly, without losing a single citation.",
        backstory="A newsroom editor turned strategy analyst. Takes notes literally and never invents facts.",
        llm=fake or make_llm(max_tokens=LONG_OUTPUT_MAX_TOKENS),  # writes a whole Brief
        allow_delegation=False,
        **AGENT_LIMITS,
    )
    task = Task(
        name="revise_brief",
        description=REVISE,
        expected_output="The whole revised Brief.",
        agent=editor,
        output_pydantic=Brief,  # no tools on this agent, so crewai sends the schema: native structured output
        guardrail=every_claim_cites_signal,  # type: ignore[arg-type]  # no dropped or invented citations
        guardrail_max_retries=1,
        human_input=human_input,
    )
    # before_kickoff: load this week's signal ids for the citation guardrail (same callback as the M3 crew).
    return Crew(
        agents=[editor],
        tasks=[task],
        process=Process.sequential,
        before_kickoff_callbacks=[register_signals],
    )


def run(
    brief: Brief,
    feedback: str,
    lessons: list[str],
    signals: str,
    human_input: bool = False,
    fake=None,
) -> Brief:
    output = build(human_input=human_input, fake=fake).kickoff(
        inputs={
            "week": brief.week,
            "brief": brief.model_dump_json(),
            "feedback": feedback.strip() or "(none given: tighten the wording, change nothing else)",
            "lessons": "\n".join(f"- {lesson}" for lesson in lessons) or "(none yet)",
            "signals": signals,
        }
    )
    assert isinstance(output, CrewOutput)
    assert output.tasks_output, "RevisionCrew produced no task output"
    return brief_from(output.tasks_output[0])
