"""Knowledge sources (spec §11.2): KiranaCloud's own documents, embedded locally for retrieval.

Knowledge vs memory: knowledge is static reference material you ship with the crew (our pricing, roadmap,
customer profile, battlecard); memory (M7) is what the system learns from its own runs.

crewai resolves a *string* file path against ./knowledge/ in the current directory; a Path is used as-is, so
these are absolute Paths from settings (tests point P3_KNOWLEDGE_DIR at a generated world).
"""

from __future__ import annotations

from crewai.knowledge.knowledge_config import KnowledgeConfig
from crewai.knowledge.source.pdf_knowledge_source import PDFKnowledgeSource
from crewai.knowledge.source.text_file_knowledge_source import TextFileKnowledgeSource

from compintel.config import get_settings

PRICING = "kiranacloud_pricing.md"
ROADMAP = "kiranacloud_roadmap_2027.md"
ICP = "ideal_customer_profile.md"
BATTLECARD = "battlecard_template.pdf"

# 3 chunks instead of crewai's default 5: they're added to every task prompt. crewai's default
# score_threshold (0.6) is kept: with the real MiniLM embedder our docs scored 0.67-0.80 on typical
# task queries.
QUERY_CONFIG = KnowledgeConfig(results_limit=3)


def text_source(*names: str) -> TextFileKnowledgeSource:
    root = get_settings().p3_knowledge_dir
    return TextFileKnowledgeSource(file_paths=[root / n for n in names])


def crew_sources() -> list:
    """Everything, for the crew: every agent (and the manager) can draw on it."""
    root = get_settings().p3_knowledge_dir
    return [text_source(PRICING, ROADMAP, ICP), PDFKnowledgeSource(file_paths=[root / BATTLECARD])]
