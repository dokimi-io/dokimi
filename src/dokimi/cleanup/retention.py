"""Whether a run keeps its evidence, and which of its test pods may stay.

A run that passed leaves nothing behind but its kept Prometheus. A run that didn't keeps
what explains it: its test pods, with their logs and events, and its owner and definition,
the record ``dokimi cleanup`` removes it by. The submitter can force either way.

Keeping evidence never holds the cluster: the run is marked finished either way. That is
only safe for a test pod that is not sending, because a later run would measure next to
it. So a pod stays only if its test is known to be quiet: it was measured, or its traffic
never started. Anything else, such as a test that timed out with its client still running,
or one that recorded nothing, has its pods removed whatever the retention says.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable

from dokimi.error_categories import ErrorCategory
from dokimi.validation import Outcome, TestOutcome

__all__ = ["QUIET_ERRORS", "Retention", "quiet_tests"]

#: Errors after which a test's tool is known not to be sending: its pods never became
#: ready, or the tool finished and only reading its result failed.
QUIET_ERRORS = frozenset(
    {
        ErrorCategory.SCHEDULING,
        ErrorCategory.IMAGE_PULL,
        ErrorCategory.METRICS_MISSING,
        ErrorCategory.EXPORTER_PARSE,
    }
)


class Retention(enum.StrEnum):
    """When a run keeps its evidence: ``submit --retain``."""

    ON_FAILURE = "on-failure"
    ALWAYS = "always"
    NEVER = "never"

    def reason(self, outcome: Outcome | None) -> str | None:
        """Why a run that came to *outcome* keeps its evidence, or ``None`` if it doesn't.

        *outcome* ``None`` is a run that stopped before it reached one, which never
        passed. The reason is what the run's owner records, so it says what happened.
        """
        if self is Retention.NEVER:
            return None
        failed = "stopped" if outcome is None else outcome.value
        if outcome is not Outcome.PASS:
            return failed
        return "requested" if self is Retention.ALWAYS else None


def quiet_tests(tests: Iterable[TestOutcome]) -> frozenset[str]:
    """The tests whose pods may be kept: measured, or never started sending."""
    quiet = set()
    for test in tests:
        if not test.recorded:
            continue
        error = test.error
        if error is None or error.category in QUIET_ERRORS:
            quiet.add(test.test_id)
    return frozenset(quiet)
