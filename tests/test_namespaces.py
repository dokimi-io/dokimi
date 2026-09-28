"""Namespaces are scoped per test type, with a configurable override."""

from __future__ import annotations

import pytest

from dokimi.k8s.namespaces import InvalidNamespaceError, namespace_for


def test_default_is_derived_from_the_test_type() -> None:
    assert namespace_for("iperf2") == "dokimi-iperf2"
    assert namespace_for("IRTT") == "dokimi-irtt"


def test_every_run_of_a_test_type_shares_one_namespace() -> None:
    assert namespace_for("iperf2") == namespace_for("iperf2")
    assert namespace_for("iperf2") != namespace_for("irtt")


def test_override_wins() -> None:
    assert namespace_for("iperf2", override="netperf-lab") == "netperf-lab"


def test_blank_override_means_no_override() -> None:
    assert namespace_for("iperf2", override="  ") == "dokimi-iperf2"


@pytest.mark.parametrize("override", ["Net_Lab", "-lab", "x" * 64])
def test_invalid_names_are_refused(override: str) -> None:
    with pytest.raises(InvalidNamespaceError):
        namespace_for("iperf2", override=override)
