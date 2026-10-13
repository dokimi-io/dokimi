"""The test-type interface's own values: parameter schemas, capabilities, images.

The interface is ADR-02-01's contract and designs/test-type-interface.md's shape. These
tests hold the parts every test type shares; ``test_iperf2.py`` holds the first plugin
to it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from dokimi.model import (
    Bandwidth,
    Comparison,
    ConfigError,
    Criterion,
    Duration,
    Metric,
    Protocol,
    TestDefinition,
    TestType,
    Unit,
)
from dokimi.testtypes import parameters_for, plugin_for
from dokimi.testtypes.images import WORKLOAD_REGISTRY, WorkloadImageError, resolve_workload_image
from dokimi.testtypes.interface import (
    Capability,
    ImageSource,
    Parameter,
    ParameterSchema,
    TestTypePlugin,
    WorkloadImage,
)
from dokimi.testtypes.iperf2 import Iperf2

DIGEST = "sha256:" + "a" * 64


@pytest.fixture
def definition() -> TestDefinition:
    return TestDefinition(
        name="worker-01-to-worker-02-udp",
        source_node="worker-01",
        target_node="worker-02",
        test_type=TestType.IPERF2,
        protocol=Protocol.UDP,
        duration=Duration.parse("30s"),
        target_bandwidth=Bandwidth.parse("1G"),
        criteria=(
            Criterion(
                metric=Metric.THROUGHPUT,
                comparison=Comparison.AT_LEAST,
                threshold=Decimal(950),
                unit=Unit.MEGABITS_PER_SECOND,
            ),
        ),
    )


def _schema() -> ParameterSchema:
    return ParameterSchema(
        test_type="example",
        parameters=(
            Parameter("rate", Decimal, "A number."),
            Parameter("mode", str, "A choice.", default="fast", choices=("fast", "slow")),
        ),
    )


def test_a_schema_fills_in_defaults_and_records_what_was_used() -> None:
    params = _schema().validate({"rate": "5"})

    assert params.get("rate", Decimal) == Decimal(5)
    assert params.written == {"rate": "5", "mode": "fast"}


def test_a_schema_refuses_unknown_parameters_rather_than_ignoring_them() -> None:
    # ADR-04-03: an ignored typo is a test that ran at the wrong rate and passed.
    with pytest.raises(ConfigError, match="does not accept the parameter.*rtae"):
        _schema().validate({"rate": "5", "rtae": "6"})


def test_a_schema_requires_parameters_without_a_default() -> None:
    with pytest.raises(ConfigError, match="requires the parameter 'rate'"):
        _schema().validate({})


def test_a_schema_refuses_a_value_outside_its_choices() -> None:
    with pytest.raises(ConfigError, match="Accepted values are: 'fast', 'slow'"):
        _schema().validate({"rate": "5", "mode": "medium"})


def test_a_schema_refuses_a_parameter_declared_twice() -> None:
    with pytest.raises(ValueError, match="more than once"):
        ParameterSchema("example", (Parameter("a", str, ""), Parameter("a", str, "")))


def test_a_parameter_value_of_the_wrong_type_is_a_bug_not_a_config_error() -> None:
    params = _schema().validate({"rate": "5"})

    with pytest.raises(TypeError):
        params.get("rate", str)


def test_every_capability_backs_a_criterion_metric() -> None:
    # The Milestone-02 pre-deployment check compares criteria to capabilities. Spelling
    # both the same makes that check a lookup, not a translation table (ADR-02-01).
    for capability in Capability:
        assert isinstance(capability.metric, Metric)
        assert capability.metric.value == capability.value


def test_a_release_default_image_is_pinned_by_digest() -> None:
    image = resolve_workload_image("iperf2", DIGEST)

    assert image.reference == f"{WORKLOAD_REGISTRY}/iperf2@{DIGEST}"
    assert image.digest == DIGEST
    assert image.source is ImageSource.RELEASE_DEFAULT


def test_the_workload_registry_is_the_renamed_organisation() -> None:
    assert WORKLOAD_REGISTRY == "ghcr.io/dokimi-io"  # ADR-01-10, superseding ADR-05-04


def test_an_override_is_used_as_given_and_says_so() -> None:
    # An air-gapped mirror may only offer a tag. It is allowed, and recorded as unpinned.
    image = resolve_workload_image("iperf2", DIGEST, "mirror.local/iperf2:2.2.1")

    assert image == WorkloadImage("mirror.local/iperf2:2.2.1", ImageSource.RUN_OVERRIDE)
    assert image.digest is None


def test_a_checkout_has_no_default_workload_image() -> None:
    # Never published, so there is nothing of this code's to run, and no tag is invented.
    with pytest.raises(WorkloadImageError, match="development build"):
        resolve_workload_image("iperf2", None)


def test_a_malformed_baked_digest_is_refused() -> None:
    with pytest.raises(WorkloadImageError, match="not a sha256 digest"):
        resolve_workload_image("iperf2", "sha256:nope")


def test_the_registry_resolves_iperf2(definition: TestDefinition) -> None:
    plugin: TestTypePlugin = plugin_for(definition.test_type, image=f"x@{DIGEST}")

    assert isinstance(plugin, Iperf2)
    assert plugin.name == definition.test_type.value


def test_a_definition_becomes_parameters_its_plugin_accepts(definition: TestDefinition) -> None:
    plugin = plugin_for(definition.test_type)
    params = plugin.parameter_schema().validate(parameters_for(definition))

    assert params.written["protocol"] == definition.protocol.value
    assert params.written["targetBandwidth"] == str(definition.target_bandwidth)
    assert params.written["duration"] == str(definition.duration)
