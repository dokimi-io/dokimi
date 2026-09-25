"""Run identity and ownership labels (ADR-02-04, ADR-05-02)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

import pytest

from test_orchestrator.k8s.identity import (
    MANAGED_BY,
    MANAGED_BY_LABEL,
    RUN_ID_LABEL,
    default_namespace,
    new_run_id,
    ownership_labels,
    run_selector,
    validate_run_id,
)
from test_orchestrator.model import ConfigError, TestType

# -- the run_id format (ADR-02-08) ---------------------------------------------
#
# These mirror M1-05's tests for the same format (#28) case for case, so the two
# implementations are held to one definition until one of them replaces the other.

_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def test_format_is_timestamp_then_random_suffix() -> None:
    run_id = new_run_id(datetime(2026, 9, 24, 14, 30, 12, tzinfo=UTC))

    assert re.fullmatch(r"20260924-143012-[0-9a-z]{6}", run_id)


def test_is_usable_verbatim_as_a_label_value_and_in_an_object_name() -> None:
    # The point of the format: one spelling everywhere, no conversion anywhere.
    run_id = new_run_id()

    assert len(run_id) == 22
    assert re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?", run_id)
    assert _DNS_LABEL.match(run_id)


def test_string_order_is_chronological_order() -> None:
    start = datetime(2026, 12, 31, 23, 59, 58, tzinfo=UTC)
    ids = [new_run_id(start + timedelta(seconds=n)) for n in range(4)]

    assert sorted(reversed(ids)) == ids


def test_the_timestamp_is_utc_whatever_the_callers_zone() -> None:
    local = datetime(2026, 9, 24, 16, 30, 12, tzinfo=timezone(timedelta(hours=2)))

    assert new_run_id(local).startswith("20260924-143012-")


def test_a_naive_datetime_is_refused_not_guessed_at() -> None:
    # The previous generator read a naive datetime as local time, silently, which is
    # exactly what ADR-02-08 says must not happen.
    with pytest.raises(ValueError, match="timezone-aware"):
        new_run_id(datetime(2026, 9, 24, 14, 30, 12))


def test_suffixes_differ_within_the_same_second() -> None:
    moment = datetime(2026, 9, 24, 14, 30, 12, tzinfo=UTC)

    assert len({new_run_id(moment) for _ in range(200)}) == 200


@pytest.mark.parametrize(
    "malformed",
    [
        "",
        "3f2a9c1e-8b7d-4e6f-a5b4-c3d2e1f0a9b8",  # a UUID is precisely what was rejected
        "20260924-143012",
        "20260924-143012-K3F9X2",  # refused, not lowercased
        "20260924-143012-k3f9x",
        "20260924T143012Z-k3f9x2",  # this branch's previous format
        "20261324-143012-k3f9x2",  # month 13
        "20260230-143012-k3f9x2",  # 30 February
        "20260924-246012-k3f9x2",  # hour 24
    ],
)
def test_a_malformed_run_id_is_a_config_error(malformed: str) -> None:
    # Reattach puts this straight into a label selector. A bad one must not come back as
    # "no such run", which reads like the run finished.
    with pytest.raises(ConfigError, match="malformed run id"):
        validate_run_id(malformed)


def test_validation_accepts_its_own_output_and_strips_only_whitespace() -> None:
    run_id = new_run_id()

    assert validate_run_id(f" {run_id}\n") == run_id


def test_ownership_labels_carry_the_two_adr_05_02_requires() -> None:
    labels = ownership_labels("20260101-100000-4f2a9c")

    assert labels[MANAGED_BY_LABEL] == MANAGED_BY
    assert labels[RUN_ID_LABEL] == "20260101-100000-4f2a9c"


def test_the_selector_scopes_to_one_run() -> None:
    selector = run_selector("20260101-100000-4f2a9c", "orchestrator")

    assert f"{RUN_ID_LABEL}=20260101-100000-4f2a9c" in selector
    assert f"{MANAGED_BY_LABEL}={MANAGED_BY}" in selector
    assert "app.kubernetes.io/component=orchestrator" in selector


def test_namespaces_are_per_test_type_not_per_run() -> None:
    first = default_namespace(TestType.IPERF2)
    second = default_namespace(TestType.IPERF2)

    assert first == second == "test-orchestrator-iperf2"
