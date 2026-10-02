"""Criteria evaluation against the result vector.

Phase 8. A criterion is a canonical record evaluated over every expected pair; an
empty result is an error, never a pass. This layer classifies the outcome, and a Robot
status is a rendering of that classification, never its source (ADR-02-05, ADR-05-05,
ADR-01-08).

**Not built yet: evaluation arrives with M1-13.** Until then :func:`evaluate` refuses,
and says so. A criterion keyword in a run's report therefore fails with that message
rather than passing, or being skipped, without anything having been checked: a test must
never look as if it passed when nothing was compared.
"""

from __future__ import annotations

from dokimi.model import Criterion

__all__ = ["NotEvaluatedError", "evaluate"]


class NotEvaluatedError(RuntimeError):
    """A criterion that this build cannot evaluate yet."""


def evaluate(criterion: Criterion, phrase: str) -> None:
    """Evaluate *criterion*, written in the suite as *phrase*.

    Raises:
        NotEvaluatedError: always, until M1-13 builds phase 8.
    """
    raise NotEvaluatedError(
        f"not evaluated: {phrase!r} was not checked, because evaluating pass criteria "
        "arrives with M1-13 (validation). This test has not passed."
    )
