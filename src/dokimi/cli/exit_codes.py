"""The exit codes a run reports to whoever invoked it.

The table is automation-facing from the first release, so the numbers are an interface,
not an implementation detail — nothing here may be renumbered to suit a later
convenience.

Codes 0 to 2 are a completed run's verdict, computed in the cluster from the outcome each
test recorded (:meth:`ExitCode.of`). The CLI's job is narrower: it produces
``CONFIG_ERROR`` itself when a definition never reaches the cluster, ``INTERNAL`` when
the submission path itself breaks, and otherwise propagates the Job's own code unchanged
— the verdict is computed where the run happened, not re-derived here.
"""

from __future__ import annotations

import enum

from dokimi.validation import Outcome

__all__ = ["ExitCode"]


class ExitCode(enum.IntEnum):
    """The exit codes, in order of precedence: the highest applicable code wins."""

    PASS = 0
    FAIL = 1
    ERROR = 2
    CONFIG_ERROR = 3
    PREFLIGHT_FAILURE = 4
    INTERNAL = 5

    #: Not a run outcome and deliberately outside the table's range: the user pressed
    #: Ctrl-C. The run keeps going in the cluster, so reporting one of the codes above
    #: would claim a verdict that does not exist yet. 130 is the shell's own convention
    #: for a process ended by SIGINT.
    INTERRUPTED = 130

    @classmethod
    def of(cls, outcome: Outcome | None) -> ExitCode:
        """A completed run's code from its outcome; ``None``, a run with none, is ours."""
        if outcome is None:
            return cls.INTERNAL
        return {Outcome.PASS: cls.PASS, Outcome.FAIL: cls.FAIL, Outcome.ERROR: cls.ERROR}[outcome]
