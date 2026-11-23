"""The YAML authoring format.

Shape follows ADR-04-03's Kubernetes-style conventions and ADR-02-05's criterion record.
ADR-04-06 scopes Milestone-01 to the fields below; the versioned JSON Schema of ADR-04-03
arrives in Milestone-02, so validation here is explicit rather than schema-driven.

**Unknown fields are rejected, never ignored.** That is ADR-04-03's reasoning and it
matters for a measurement tool: a silently ignored typo in ``targetBandwidth`` produces a
test that ran at the wrong rate and reported success.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import yaml

from dokimi.model import (
    DEFAULT_RESULTS_DIRECTORY,
    Bandwidth,
    Comparison,
    ConfigError,
    Criterion,
    Duration,
    Metric,
    MonitoringMode,
    Protocol,
    Statistic,
    TestDefinition,
    TestType,
    Unit,
)

__all__ = ["parse_yaml", "render_yaml"]

API_VERSION = "dokimi.io/v1alpha1"
KIND = "NetworkTest"

_TOP_LEVEL = {
    "apiVersion",
    "kind",
    "metadata",
    "placement",
    "test",
    "monitoring",
    "validation",
    "results",
}
_METADATA = {"name"}
_PLACEMENT = {"mode", "source", "target"}
_TEST = {"type", "protocol", "duration", "targetBandwidth"}
_CRITERION = {"metric", "statistic", "comparison", "threshold", "unit"}
_MONITORING = {"mode"}
_RESULTS = {"directory"}


def parse_yaml(source: str) -> TestDefinition:
    """Parse a YAML test definition into the canonical model."""
    try:
        loaded = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        raise ConfigError(f"the document is not valid YAML: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ConfigError("the document must be a mapping at the top level")

    document: dict[str, Any] = loaded
    _reject_unknown(document, _TOP_LEVEL, "the document")

    api_version = _require(document, "apiVersion", "the document")
    if api_version != API_VERSION:
        raise ConfigError(f"unsupported apiVersion {api_version!r}, expected {API_VERSION!r}")
    kind = _require(document, "kind", "the document")
    if kind != KIND:
        raise ConfigError(f"unsupported kind {kind!r}, expected {KIND!r}")

    metadata = _mapping(document, "metadata", "the document")
    _reject_unknown(metadata, _METADATA, "metadata")

    placement = _mapping(document, "placement", "the document")
    _reject_unknown(placement, _PLACEMENT, "placement")
    mode = _require(placement, "mode", "placement")
    if mode != "explicit":
        raise ConfigError(
            f"placement mode {mode!r} is not available in this version, which resolves "
            "two explicitly named nodes. Selector-based modes are planned"
        )

    test = _mapping(document, "test", "the document")
    _reject_unknown(test, _TEST, "test")

    monitoring = document.get("monitoring") or {}
    if not isinstance(monitoring, dict):
        raise ConfigError("'monitoring' must be a mapping")
    _reject_unknown(monitoring, _MONITORING, "monitoring")

    results = document.get("results") or {}
    if not isinstance(results, dict):
        raise ConfigError("'results' must be a mapping")
    _reject_unknown(results, _RESULTS, "results")

    criteria = document.get("validation")
    if not isinstance(criteria, list) or not criteria:
        raise ConfigError("'validation' must be a non-empty list of criteria")

    return TestDefinition(
        name=str(_require(metadata, "name", "metadata")),
        source_node=str(_require(placement, "source", "placement")),
        target_node=str(_require(placement, "target", "placement")),
        test_type=TestType.parse(str(_require(test, "type", "test"))),
        protocol=Protocol.parse(str(_require(test, "protocol", "test"))),
        duration=Duration.parse(str(_require(test, "duration", "test"))),
        target_bandwidth=Bandwidth.parse(str(_require(test, "targetBandwidth", "test"))),
        criteria=tuple(_criterion(entry, index) for index, entry in enumerate(criteria)),
        monitoring_mode=(
            MonitoringMode.parse(str(monitoring["mode"]))
            if "mode" in monitoring
            else MonitoringMode.AUTO
        ),
        results_directory=str(results.get("directory") or DEFAULT_RESULTS_DIRECTORY),
    )


def render_yaml(definition: TestDefinition) -> str:
    """Render the canonical model back to a YAML document.

    Not required by Milestone-01, which only has to render Robot. It exists because
    ADR-04-05 makes conversion a composition of an existing parse and an existing render,
    and because it is what makes the cross-format criterion property testable in both
    directions rather than only one.

    Every value goes through the YAML emitter rather than into an f-string, so quoting is
    the emitter's job: a name like ``a: b`` comes out quoted instead of as a document that
    does not parse. Sections are emitted one at a time only to keep the blank line
    between them, which is how the documented example is laid out.
    """
    sections: list[dict[str, Any]] = [
        {"apiVersion": API_VERSION, "kind": KIND},
        {"metadata": {"name": definition.name}},
        {
            "placement": {
                "mode": "explicit",
                "source": definition.source_node,
                "target": definition.target_node,
            }
        },
        {
            "test": {
                "type": definition.test_type.value,
                "protocol": definition.protocol.value,
                "duration": str(definition.duration),
                "targetBandwidth": str(definition.target_bandwidth),
            }
        },
        {"monitoring": {"mode": definition.monitoring_mode.value}},
        {"validation": [_criterion_record(criterion) for criterion in definition.criteria]},
        {"results": {"directory": definition.results_directory}},
    ]
    return "\n".join(
        yaml.dump(section, Dumper=_Dumper, sort_keys=False, default_flow_style=False)
        for section in sections
    )


def _criterion_record(criterion: Criterion) -> dict[str, Any]:
    record: dict[str, Any] = {"metric": criterion.metric.value}
    if criterion.statistic is not None:
        record["statistic"] = criterion.statistic.value
    record["comparison"] = criterion.comparison.value
    record["threshold"] = criterion.threshold
    record["unit"] = criterion.unit.value
    return record


class _Dumper(yaml.SafeDumper):
    """The safe dumper, plus :class:`~decimal.Decimal` written as the number it is.

    A threshold is a Decimal so the number the user wrote survives unchanged. Emitting
    it as a float would reintroduce the binary-representation noise the Decimal exists
    to keep out; emitting it as a string would quote it.
    """

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        # Indent sequence items under their key, as the documented example does.
        super().increase_indent(flow, False)


def _represent_decimal(dumper: yaml.SafeDumper, value: Decimal) -> yaml.ScalarNode:
    tag = "int" if value == value.to_integral_value() and "E" not in str(value) else "float"
    return dumper.represent_scalar(f"tag:yaml.org,2002:{tag}", str(value))


_Dumper.add_representer(Decimal, _represent_decimal)


def _criterion(entry: object, index: int) -> Criterion:
    where = f"validation[{index}]"
    if not isinstance(entry, dict):
        raise ConfigError(f"{where} must be a mapping")
    _reject_unknown(entry, _CRITERION, where)

    statistic_value = entry.get("statistic")
    statistic = None
    if statistic_value is not None:
        statistic = _enum(Statistic, str(statistic_value), "statistic", where)

    return Criterion(
        metric=Metric.parse(str(_require(entry, "metric", where))),
        comparison=_enum(
            Comparison, str(_require(entry, "comparison", where)), "comparison", where
        ),
        threshold=_decimal(_require(entry, "threshold", where), where),
        unit=Unit.parse(str(_require(entry, "unit", where))),
        statistic=statistic,
    )


def _reject_unknown(mapping: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ConfigError(
            f"unknown field(s) in {where}: {', '.join(repr(k) for k in unknown)}. "
            f"Accepted fields are: {', '.join(repr(k) for k in sorted(allowed))}"
        )


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping or mapping[key] is None:
        raise ConfigError(f"{where} is missing required field {key!r}")
    return mapping[key]


def _mapping(document: dict[str, Any], key: str, where: str) -> dict[str, Any]:
    value = _require(document, key, where)
    if not isinstance(value, dict):
        raise ConfigError(f"{key!r} must be a mapping")
    return value


def _enum(enum_cls: Any, text: str, field_name: str, where: str) -> Any:
    for member in enum_cls:
        if member.value == text:
            return member
    raise ConfigError(
        f"{where}: unknown {field_name} {text!r}. Accepted values are: "
        + ", ".join(repr(m.value) for m in enum_cls)
    )


def _decimal(value: object, where: str) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ConfigError(f"{where}: threshold {value!r} is not a number") from exc
