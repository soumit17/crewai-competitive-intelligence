"""CompIntel's own eval harness (spec §17.1, M8c). Scores the published weeks against ground truth.

Run from the project root (`evals` is a top-level package, next to `src/`):
    uv run python -m evals run              # deterministic metrics + Haiku judges (~$0.01)
    uv run python -m evals run --no-judge   # deterministic metrics only, $0
    uv run python -m evals run --final      # brief quality judged by Sonnet (once, ~$0.05)
    uv run python -m evals human            # your blind 1-5 scores, in a file you fill in
    uv run python -m evals run --save-baseline / --gate

Modules: data (what a published week is), signals (recall, precision, routing), faithfulness (ids, judge,
rumor), quality (rubric judge, your scores), compare (the M4 process comparison), scoring (one 0-100 table,
the baseline and the regression gate).
"""
