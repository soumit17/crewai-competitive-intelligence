"""Everything crew.jsonc and agents/*.jsonc may reference with {"python": "crew_refs.<name>"}.

crewai's JSON loader only imports Python references from files under the crew file's own folder (its
"project root"), so this module is the crew's whole Python surface. It re-exports the real objects, so a
model class here *is* compintel.models.Brief (no second copy of the class).
"""

from typing import Any

from compintel import llms
from compintel.crews.short_update import save_draft
from compintel.guardrails import (
    every_claim_cites_signal,
    low_credibility_only_discounted,
    register_signals,
    threat_levels_consistent,
)
from compintel.models import Brief, ThreatAssessments

__all__ = [
    "Brief",
    "ThreatAssessments",
    "every_claim_cites_signal",
    "low_credibility_only_discounted",
    "register_signals",
    "save_draft",
    "threat_levels_consistent",
]


def __getattr__(name: str) -> Any:
    # Built on access, so each load_crew() picks up the current settings (tests point them at tmp dirs).
    if name == "checkpoint":
        from compintel.crews.short_update import checkpoint_config

        return checkpoint_config()
    # "crew_refs.analyst" / "crew_refs.writer": built by compintel.llms (config.make_llm).
    return getattr(llms, name)
