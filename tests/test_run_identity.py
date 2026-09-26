"""The ``run_id`` format: sortable, readable, and valid everywhere it is used as-is."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

import pytest

from dokimi.run_identity import InvalidRunIdError, RunId, new_run_id, parse_run_id

# What Kubernetes accepts as a label value, and as a segment of a DNS-1123 object name.
_LABEL_VALUE = re.compile(r"^(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?$")
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def test_format_is_timestamp_then_random_suffix() -> None:
    run_id = new_run_id(datetime(2026, 9, 24, 14, 30, 12, tzinfo=UTC))

    assert re.fullmatch(r"20260924-143012-[0-9a-z]{6}", str(run_id))


def test_is_usable_verbatim_as_a_label_value_and_in_an_object_name() -> None:
    value = str(new_run_id())

    assert len(value) <= 63
    assert _LABEL_VALUE.match(value)
    assert _DNS_LABEL.match(value)


def test_string_order_is_chronological_order() -> None:
    start = datetime(2026, 12, 31, 23, 59, 58, tzinfo=UTC)
    ids = [new_run_id(start + timedelta(seconds=n)) for n in range(4)]

    assert sorted(str(run_id) for run_id in reversed(ids)) == [str(run_id) for run_id in ids]
    assert sorted(reversed(ids)) == ids


def test_timestamp_is_utc_whatever_the_callers_zone() -> None:
    local = datetime(2026, 9, 24, 16, 30, 12, tzinfo=timezone(timedelta(hours=2)))

    assert str(new_run_id(local)).startswith("20260924-143012-")


def test_naive_datetimes_are_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        new_run_id(datetime(2026, 9, 24, 14, 30, 12))


def test_started_at_round_trips() -> None:
    moment = datetime(2026, 9, 24, 14, 30, 12, tzinfo=UTC)

    assert new_run_id(moment).started_at == moment


def test_suffixes_differ_within_the_same_second() -> None:
    moment = datetime(2026, 9, 24, 14, 30, 12, tzinfo=UTC)

    assert len({str(new_run_id(moment)) for _ in range(200)}) == 200


@pytest.mark.parametrize(
    "value",
    [
        "",
        "3f2a9c1e-8b7d-4e6f-a5b4-c3d2e1f0a9b8",  # a UUID is precisely what was rejected
        "20260924-143012",
        "20260924-143012-K3F9X2",
        "20260924-143012-k3f9x",
        "20260924T143012Z-k3f9x2",
        "20261324-143012-k3f9x2",  # month 13
    ],
)
def test_malformed_values_are_rejected(value: str) -> None:
    with pytest.raises(InvalidRunIdError):
        parse_run_id(value)


def test_parse_accepts_its_own_output() -> None:
    run_id = new_run_id()

    assert parse_run_id(f" {run_id}\n") == RunId(str(run_id))
