"""The exit codes a run reports to whoever invoked it.

ADR-01-08 fixes this table. It is automation-facing from the first release, so the
numbers are an interface, not an implementation detail — nothing here may be renumbered
to suit a later convenience.

M1-16 (#51) owns outcome classification and will produce codes 0 to 2 from the
categories a completed run computed in-cluster. The CLI's job is narrower: it produces
``CONFIG_ERROR`` itself when a definition never reaches the cluster, ``INTERNAL`` when
the submission path itself breaks, and otherwise propagates the Job's own code unchanged
— the verdict is computed where the run happened, not re-derived here.
"""

from __future__ import annotations

import enum

__all__ = ["ExitCode"]


class ExitCode(enum.IntEnum):
    """ADR-01-08's table. The highest applicable code wins."""

    PASS = 0
    FAIL = 1
    ERROR = 2
    CONFIG_ERROR = 3
    PREFLIGHT_FAILURE = 4
    INTERNAL = 5

    #: Not a run outcome and deliberately outside ADR-01-08's range: the user pressed
    #: Ctrl-C. The run keeps going in the cluster, so reporting one of the codes above
    #: would claim a verdict that does not exist yet. 130 is the shell's own convention
    #: for a process ended by SIGINT.
    INTERRUPTED = 130
