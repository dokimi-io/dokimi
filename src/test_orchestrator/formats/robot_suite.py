"""The Robot authoring format: one parser, and the renderer every run uses.

Two different things live here, and the difference matters:

* :func:`render_robot` is **pure text generation** and needs no Robot Framework at all.
  It is phase 5 of every run (ADR-01-08) and the user-facing conversion command, and
  ADR-04-05 requires those to be the same function — "phase 5 must never be a second,
  drifting renderer".
* :func:`parse_robot` needs Robot's own parsing API. That import is **deferred into the
  function** so that importing this module, and therefore the core library, still works
  with Robot Framework absent (ADR-01-02). Calling it without Robot installed fails with
  a message that says what to install.

The suite this renders is deliberately thin: parameters and pass criteria only, with
deployment, PromQL and cleanup behind the resource file (ADR-01-03, kept by ADR-04-05 as
the Robot format's design).
"""

from __future__ import annotations

import io
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from test_orchestrator.model import (
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

__all__ = ["parse_robot", "render_robot"]

RESOURCE_IMPORT = "resources/TestOrchestrator.resource"

_VARIABLES = {
    "SOURCE_NODE": "source_node",
    "TARGET_NODE": "target_node",
    "TEST_TYPE": "test_type",
    "PROTOCOL": "protocol",
    "TEST_DURATION": "duration",
    "TARGET_BANDWIDTH": "target_bandwidth",
}

#: Carried through the suite so a Robot-authored test can say the same things a YAML one
#: can. Optional, because both have a decided default (ADR-03-02, ADR-05-09).
_OPTIONAL_VARIABLES = ("MONITORING_MODE", "RESULTS_DIRECTORY")

_SHOULD_BE = re.compile(r"^(?P<subject>.+?)\s+Should Be$")


def render_robot(definition: TestDefinition) -> str:
    """Render the canonical model as the ``.robot`` suite that is executed."""
    columns = [_criterion_columns(criterion) for criterion in definition.criteria]
    widths = [max((len(row[i]) for row in columns), default=0) for i in range(3)]

    lines = [
        "*** Settings ***",
        f"Documentation       {definition.name}",
        f"Resource            {RESOURCE_IMPORT}",
        "",
        "*** Variables ***",
        f"${{SOURCE_NODE}}         {definition.source_node}",
        f"${{TARGET_NODE}}         {definition.target_node}",
        f"${{TEST_TYPE}}           {definition.test_type.value}",
        f"${{PROTOCOL}}            {definition.protocol.value}",
        f"${{TEST_DURATION}}       {definition.duration}",
        f"${{TARGET_BANDWIDTH}}    {definition.target_bandwidth}",
        f"${{MONITORING_MODE}}     {definition.monitoring_mode.value}",
        f"${{RESULTS_DIRECTORY}}   {definition.results_directory}",
        "",
        "*** Test Cases ***",
        definition.name,
        "    Run Network Test",
        "    ...    type=${TEST_TYPE}",
        "    ...    protocol=${PROTOCOL}",
        "    ...    source=${SOURCE_NODE}",
        "    ...    target=${TARGET_NODE}",
        "    ...    bandwidth=${TARGET_BANDWIDTH}",
        "    ...    duration=${TEST_DURATION}",
        "",
    ]
    for row in columns:
        padded = [cell.ljust(width) for cell, width in zip(row[:3], widths, strict=True)]
        lines.append(("    " + "    ".join([*padded, row[3]])).rstrip())
    return "\n".join(lines) + "\n"


def parse_robot(source: str) -> TestDefinition:
    """Parse a ``.robot`` suite into the canonical model.

    Requires Robot Framework. The import is deferred so that the core library stays
    importable without it.
    """
    model = _parse_with_robot(source)

    variables: dict[str, str] = {}
    criteria: list[Criterion] = []
    documentation: list[str] = []
    test_names: list[str] = []

    for statement, section in _walk(model):
        kind = type(statement).__name__
        if kind == "Variable":
            name, value = _variable(statement)
            if name is not None:
                variables[name] = value
        elif kind == "Documentation":
            documentation.append(_documentation(statement))
        elif kind == "TestCaseName":
            test_names.append(statement.name.strip())
        elif kind == "KeywordCall" and section == "testcase":
            criterion = _criterion_from_call(statement)
            if criterion is not None:
                criteria.append(criterion)

    missing = sorted(set(_VARIABLES) - set(variables))
    if missing:
        raise ConfigError(
            "the suite is missing required variable(s): "
            + ", ".join(f"${{{name}}}" for name in missing)
        )

    name = next((doc for doc in documentation if doc), None) or (
        test_names[0] if test_names else ""
    )

    return TestDefinition(
        name=name,
        source_node=variables["SOURCE_NODE"],
        target_node=variables["TARGET_NODE"],
        test_type=TestType.parse(variables["TEST_TYPE"]),
        protocol=Protocol.parse(variables["PROTOCOL"]),
        duration=Duration.parse(variables["TEST_DURATION"]),
        target_bandwidth=Bandwidth.parse(variables["TARGET_BANDWIDTH"]),
        criteria=tuple(criteria),
        monitoring_mode=(
            MonitoringMode.parse(variables["MONITORING_MODE"])
            if variables.get("MONITORING_MODE")
            else MonitoringMode.AUTO
        ),
        results_directory=variables.get("RESULTS_DIRECTORY") or DEFAULT_RESULTS_DIRECTORY,
    )


def _criterion_columns(criterion: Criterion) -> tuple[str, str, str, str]:
    """The four columns of the documented surface, unpadded.

    ``<Statistic>? <Metric> Should Be <Comparison> <Value> <Unit>`` (ADR-02-05). The
    caller pads them so the columns line up; the grammar is what matters here.
    """
    subject = criterion.metric.robot_phrasing
    if criterion.statistic is not None:
        subject = f"{criterion.statistic.robot_phrasing} {subject}"
    return (
        f"{subject} Should Be",
        criterion.comparison.robot_phrasing,
        str(criterion.threshold),
        criterion.unit.value,
    )


def _criterion_from_call(statement: Any) -> Criterion | None:
    keyword = (getattr(statement, "keyword", None) or "").strip()
    match = _SHOULD_BE.match(keyword)
    if match is None:
        return None

    arguments = [str(argument) for argument in getattr(statement, "args", ())]
    if len(arguments) < 3:
        raise ConfigError(
            f"criterion {keyword!r} needs a comparison, a value and a unit, "
            f"got {len(arguments)} argument(s)"
        )
    comparison_text, threshold_text, unit_text = arguments[0], arguments[1], " ".join(arguments[2:])

    statistic, metric = _subject(match.group("subject").strip(), keyword)
    return Criterion(
        metric=metric,
        comparison=_comparison(comparison_text, keyword),
        threshold=_threshold(threshold_text, keyword),
        unit=Unit.parse(unit_text),
        statistic=statistic,
    )


def _subject(subject: str, keyword: str) -> tuple[Statistic | None, Metric]:
    for statistic in Statistic:
        prefix = statistic.robot_phrasing + " "
        if subject.startswith(prefix):
            return statistic, _metric(subject[len(prefix) :], keyword)
    return None, _metric(subject, keyword)


def _metric(phrasing: str, keyword: str) -> Metric:
    for metric in Metric:
        if metric.robot_phrasing == phrasing:
            return metric
    raise ConfigError(
        f"criterion {keyword!r} names an unknown metric {phrasing!r}. "
        "Accepted metrics are: " + ", ".join(repr(m.robot_phrasing) for m in Metric)
    )


def _comparison(text: str, keyword: str) -> Comparison:
    for comparison in Comparison:
        if comparison.robot_phrasing == text.strip():
            return comparison
    raise ConfigError(
        f"criterion {keyword!r} has an unknown comparison {text!r}. "
        "Accepted comparisons are: " + ", ".join(repr(c.robot_phrasing) for c in Comparison)
    )


def _threshold(text: str, keyword: str) -> Decimal:
    try:
        return Decimal(text.strip())
    except InvalidOperation as exc:
        raise ConfigError(f"criterion {keyword!r} has a non-numeric value {text!r}") from exc


def _variable(statement: Any) -> tuple[str | None, str]:
    raw_name = (getattr(statement, "name", None) or "").strip()
    name = raw_name.removeprefix("${").removesuffix("}").removesuffix("=").strip()
    values = [str(value) for value in getattr(statement, "value", ())]
    return (name or None), (values[0] if values else "")


def _documentation(statement: Any) -> str:
    value = getattr(statement, "value", "")
    if isinstance(value, str):
        return value.strip()
    return " ".join(str(part) for part in value).strip()


def _walk(model: Any) -> list[tuple[Any, str]]:
    """Flatten the parsed suite into (statement, section) pairs.

    Robot's model is a tree: the ``File`` holds ``sections``, each section holds a
    ``header`` and a ``body``, and a ``TestCase`` holds its own ``body``. Walking it by
    attribute rather than through the visitor API keeps this plugin independent of the
    visitor's shape across Robot versions.

    Nodes are typed ``Any`` deliberately. This is someone else's dynamically shaped AST,
    and pretending otherwise would mean asserting a structure this code does not own.
    """
    found: list[tuple[Any, str]] = []

    def visit(node: Any, section: str) -> None:
        if type(node).__name__ == "TestCaseSection":
            section = "testcase"
        for attribute in ("sections", "header", "body"):
            child = getattr(node, attribute, None)
            if child is None:
                continue
            for item in child if isinstance(child, list) else [child]:
                found.append((item, section))
                visit(item, section)

    visit(model, "")
    return found


def _parse_with_robot(source: str) -> Any:
    """Hand the suite to Robot's own parser.

    The import lives here rather than at module scope so that importing this module,
    and therefore the core library, still works with Robot Framework absent
    (ADR-01-02). Only this function needs it; rendering does not.
    """
    try:
        from robot.api import get_model  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised by the no-Robot CI job
        raise ConfigError(
            "reading a .robot suite needs Robot Framework, which is not installed. "
            "Install the 'robot' extra (uv sync --extra robot), or author the test as "
            "YAML instead — both formats parse to the same model."
        ) from exc

    # Robot's parser takes a path or a file-like object, never a bare string.
    return get_model(io.StringIO(source))
