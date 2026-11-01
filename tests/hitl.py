"""$0 stand-ins for the human reviewer and for the model behind @human_feedback (feedback → outcome, lesson
distillation, pre-review) and Memory (field inference, recall). Shared by the flow and HITL tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from compintel.models import Brief
from tests.fakes import Rule, ScriptedLLM, memory_reply

APPROVE = ("Approved, looks good.", "approve")


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0].strip()


class ScriptedReviewer:
    """A crewai HumanFeedbackProvider answering from (feedback, outcome) pairs; then it approves."""

    def __init__(self, *answers: tuple[str, str]) -> None:
        self.answers = list(answers)
        self.outcome_of = dict([APPROVE, *answers])  # what the scripted model maps each feedback text to
        self.shown: list[Brief] = []  # what the reviewer was shown, round by round

    def request_feedback(self, context: Any, flow: Any) -> str:
        output = context.method_output
        self.shown.append(output if isinstance(output, Brief) else Brief.model_validate(output))
        return (self.answers.pop(0) if self.answers else APPROVE)[0]


def lead_with_kolkata(brief: Brief, lessons: str) -> Brief:
    """What the scripted pre-review does with a lesson about Kolkata: lead with it."""
    if "Kolkata" not in lessons or brief.headline.startswith("Kolkata"):
        return brief
    return brief.model_copy(update={"headline": f"Kolkata: {brief.headline}"})


def hitl_llm(reviewer: ScriptedReviewer, pre_review: Callable[[Brief, str], Brief] = lead_with_kolkata):
    def answer(messages: list[Any], response_model: Any) -> Any:
        prompt = str(messages[-1]["content"])
        name = getattr(response_model, "__name__", "")
        if name == "FeedbackOutcome":  # the collapse of free text to an emit outcome
            return {"outcome": reviewer.outcome_of[_between(prompt, "Feedback:", "\n\nPossible outcomes")]}
        if name == "DistilledLessons":  # a change request is a lesson; an approval isn't
            feedback = _between(prompt, "Human feedback:", "\n\nExtract")
            return {"lessons": [] if reviewer.outcome_of.get(feedback) == "approve" else [feedback]}
        if response_model is Brief:  # the learn=True pre-review of the Brief
            brief = Brief.model_validate_json(_between(prompt, "Output to review:", "\n\nLessons from past"))
            return pre_review(brief, _between(prompt, "Lessons from past human feedback:", "\n\nApply"))
        return memory_reply(messages, response_model)  # Memory's own calls (save analysis, extraction)

    return ScriptedLLM(Rule(answer))


def install(monkeypatch, reviewer: ScriptedReviewer, llm: ScriptedLLM | None = None) -> ScriptedLLM:
    """Review with `reviewer`; every model call the review makes (and Memory's) goes to the scripted LLM."""
    import importlib

    # importlib: crewai.flow re-exports the decorator under the module's name, so `import ... as` gets the
    # function.
    hf = importlib.import_module("crewai.flow.human_feedback")
    runtime = importlib.import_module("crewai.flow.runtime")

    from compintel import memory, review

    llm = llm or hitl_llm(reviewer)
    # Where crewai turns @human_feedback(llm=...) into the model it calls (collapse, distill, pre-review).
    monkeypatch.setattr(runtime, "_resolve_llm_instance", lambda _llm: llm)
    monkeypatch.setattr(hf, "_resolve_llm_instance", lambda _llm: llm)
    monkeypatch.setattr(memory, "make_llm", lambda **_: llm)
    review.use(reviewer)
    return llm
