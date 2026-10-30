"""Generate the 3-week mock world (spec §4.5). Deterministic: rerunning gives identical files.

uv run python scripts/gen_world.py
"""

from compintel.config import PROJECT_ROOT
from compintel.world.generate import generate

if __name__ == "__main__":
    generate(PROJECT_ROOT)
    print(f"wrote {PROJECT_ROOT / 'data'} and {PROJECT_ROOT / 'knowledge'}")
