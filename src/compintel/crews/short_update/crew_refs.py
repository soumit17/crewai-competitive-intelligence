"""Everything crew.jsonc and agents/*.jsonc may reference with {"python": "crew_refs.<name>"}.

crewai's JSON loader only imports Python references from files under the crew file's own folder (its
"project root"), so this module is the crew's whole Python surface. It re-exports the real objects, so a
model class here *is* compintel.models.Brief (no second copy of the class).
"""

from typing import Any

from compintel import llms
from compintel.crews.short_update import save_draft
from compintel.guardrails import every_claim_cites_signal, low_credibility_only_discounted, register_signals
from compintel.models import Brief, ThreatAssessments

__all__ = [
    "Brief",
    "ThreatAssessments",
    "every_claim_cites_signal",
    "low_credibility_only_discounted",
    "register_signals",
    "save_draft",
]


def __getattr__(name: str) -> Any:
    # "crew_refs.analyst" / "crew_refs.writer": built on access by compintel.llms (config.make_llm).
    return getattr(llms, name)
