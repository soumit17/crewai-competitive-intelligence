"""Process B of the cross-process review test: answer a review that process A (pytest) left pending.

Run as `python -m tests.resume_review <flow_id> <feedback>` with process A's env and cwd. Same $0 fakes as in
pytest: scripted review model, hash embedder. Prints one JSON line about the result.
"""

from __future__ import annotations

import json
import sys

import pytest

from compintel import review
from compintel.config import embedder_scope
from tests import hitl
from tests.fakes import FAKE_EMBEDDER_SPEC


def main(flow_id: str, feedback: str) -> None:
    with pytest.MonkeyPatch.context() as mp, embedder_scope(FAKE_EMBEDDER_SPEC):
        hitl.install(mp, hitl.ScriptedReviewer())
        flow, _result = review.resume(flow_id, feedback)
        print(json.dumps({"state_id": flow.state.id, "published": flow.state.published_path}))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
