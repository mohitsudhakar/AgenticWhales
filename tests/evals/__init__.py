"""Evaluation harnesses for AgenticWhales.

This subpackage holds *measurement* code (as opposed to *correctness* tests
in the parent `tests/` tree). Eval harnesses answer questions like:

  - Does synthesizer-family diversity reduce Brier vs all-same-family?
  - Does layered memory beat flat top-K on alpha vs SPY?
  - Does the Diversity Engine's classical injection improve outcomes when
    Bull/Bear agreement is high?

These are not unit tests — they run a small fixture set of resolved
ticker-date pairs against different configurations of the graph and
compare metrics (Brier component, hit rate, alpha vs SPY).

Default pytest run **excludes** this directory (marker `eval`). To run:

    pytest tests/evals -m eval
"""
