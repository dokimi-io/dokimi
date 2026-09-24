"""Run identity: the ``run_id`` every other part of a run is joined on.

[ADR-02-07] (restating [ADR-02-04]) makes ``run_id`` the join key of the whole
architecture — the same value labels the Prometheus series, the Kubernetes resources and
the result artifact rows. It is generated once at setup and stable for the whole run.

The accepted format is "a sortable ``<timestamp>-<short-random>``, not a UUID". The exact
spec was left as follow-up work; this module settles it::

    20260924-143012-k3f9x2
    ^^^^^^^^ ^^^^^^ ^^^^^^
    date     time   random suffix
    (UTC)    (UTC, seconds)

* **UTC, second precision, fixed width.** Fixed width is what makes plain string order
  equal chronological order, which is the property the format was chosen for.
* **Six characters of** ``[0-9a-z]``, from :mod:`secrets` — 36**6, about 2.2 billion
  values per second. Two runs cannot legitimately start in the same second anyway (run
  exclusivity, [ADR-05-02]); the suffix exists so that a refused second submission and the
  run it lost to are still told apart in logs.
* **Lowercase, digits and hyphens only**, 22 characters. That is simultaneously a valid
  Kubernetes label value, a valid part of a DNS-1123 object name, a valid Prometheus label
  value and a CSV cell that needs no quoting — one spelling everywhere, no escaping.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

__all__ = [
    "RUN_ID_PATTERN",
    "InvalidRunIdError",
    "RunId",
    "new_run_id",
    "parse_run_id",
]

_SUFFIX_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_SUFFIX_LENGTH = 6
_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"

RUN_ID_PATTERN = re.compile(r"^(?P<timestamp>\d{8}-\d{6})-(?P<suffix>[0-9a-z]{6})$")


class InvalidRunIdError(ValueError):
    """A string that is not a ``run_id`` in the format this module defines."""


@dataclass(frozen=True, order=True)
class RunId:
    """A validated ``run_id``. Orders chronologically, because its string does."""

    value: str

    def __post_init__(self) -> None:
        match = RUN_ID_PATTERN.match(self.value)
        if match is None:
            raise InvalidRunIdError(
                f"{self.value!r} is not a run_id: expected YYYYMMDD-HHMMSS-xxxxxx "
                "(UTC timestamp, then six characters of [0-9a-z])"
            )
        try:
            datetime.strptime(match["timestamp"], _TIMESTAMP_FORMAT)
        except ValueError as error:
            raise InvalidRunIdError(
                f"{self.value!r} is not a run_id: its timestamp is not a real date and time"
            ) from error

    @property
    def started_at(self) -> datetime:
        """The UTC start time the identifier encodes, to the second."""
        timestamp = self.value[: -_SUFFIX_LENGTH - 1]
        return datetime.strptime(timestamp, _TIMESTAMP_FORMAT).replace(tzinfo=UTC)

    def __str__(self) -> str:
        return self.value


def new_run_id(now: datetime | None = None) -> RunId:
    """Generate a fresh ``run_id``. Call once per run, at setup, and pass it along.

    *now* is for tests; a naive datetime is rejected rather than guessed at, since a
    local-time timestamp would silently break the ordering across machines.
    """
    moment = datetime.now(UTC) if now is None else now
    if moment.tzinfo is None:
        raise ValueError("new_run_id needs a timezone-aware datetime")
    timestamp = moment.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)
    suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(_SUFFIX_LENGTH))
    return RunId(f"{timestamp}-{suffix}")


def parse_run_id(value: str) -> RunId:
    """Validate a ``run_id`` given from outside — a CLI argument, a label, a CSV cell."""
    return RunId(value.strip())
