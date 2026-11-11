"""The process comparison (spec §8.3, M4), carried into the eval report. It was a one-off live experiment on
week 2 (one run per process), not something every eval re-runs: the table comes from the committed
docs/process-comparison.md, between its table markers."""

from __future__ import annotations

from compintel.config import PROJECT_ROOT

DOC = PROJECT_ROOT / "docs" / "process-comparison.md"


def table() -> str:
    text = DOC.read_text() if DOC.exists() else ""
    if "<!-- table:begin -->" not in text:
        return "(no process comparison yet: scripts/process_comparison.py)"
    return text.split("<!-- table:begin -->", 1)[1].split("<!-- table:end -->", 1)[0].strip()
