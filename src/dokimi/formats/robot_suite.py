"""The Robot authoring format: one parser, and the renderer every run uses.

Two different things live here, and the difference matters:

* :func:`render_robot` is **pure text generation** and needs no Robot Framework at all.
  It is phase 5 of every run and the user-facing conversion command, and those are the
  same function so that phase 5 can never become a second, drifting renderer.
* :func:`parse_robot` needs Robot's own parsing API. That import is **deferred into the
  function** so that importing this module, and therefore the core library, still works
  with Robot Framework absent. Calling it without Robot installed fails with
  a message that says what to install.

The suite this renders is deliberately thin: parameters and pass criteria only, with
deployment, PromQL and cleanup behind the resource file.

**The parser accepts exactly what the renderer emits, and rejects everything else.** The
suite that executes is never the one the user wrote: it is ``render_robot()`` of the
parsed model. So any construct the parser steps over — a criterion in an
``ELSE`` branch, a lowercased keyword, a ``[Teardown]``, a stray variable — is a construct
that silently never runs, and a dropped criterion is a run that passes without checking
it. Rejecting is the only safe answer that does not require interpreting Robot's
semantics, and it is the YAML format's rule (unknown fields are rejected, never
ignored) applied to the other format.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

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

__all__ = [
    "RUN_NETWORK_TEST",
    "criterion_from_keyword",
    "criterion_keyword_names",
    "parse_robot",
    "render_robot",
]

RESOURCE_IMPORT = "resources/Dokimi.resource"

_VARIABLES = {
    "SOURCE_NODE": "source_node",
    "TARGET_NODE": "target_node",
    "TEST_TYPE": "test_type",
    "PROTOCOL": "protocol",
    "TEST_DURATION": "duration",
    "TARGET_BANDWIDTH": "target_bandwidth",
}

#: Carried through the suite so a Robot-authored test can say the same things a YAML one
#: can. Optional, because both have a decided default.
_OPTIONAL_VARIABLES = ("MONITORING_MODE", "RESULTS_DIRECTORY")

#: The arguments ``Run Network Test`` is rendered with. They reference the variables and
#: nothing else, because the variables are what the parser reads: a literal like
#: ``source=worker-09`` here would be silently overridden by ``${SOURCE_NODE}``.
#: The keyword a test case calls to run its pair. The Robot adapter implements it.
RUN_NETWORK_TEST = "Run Network Test"
_RUN_NETWORK_TEST = RUN_NETWORK_TEST
_RUN_NETWORK_TEST_ARGUMENTS = frozenset(
    {
        "type=${TEST_TYPE}",
        "protocol=${PROTOCOL}",
        "source=${SOURCE_NODE}",
        "target=${TARGET_NODE}",
        "bandwidth=${TARGET_BANDWIDTH}",
        "duration=${TEST_DURATION}",
    }
)

#: Statement types that carry no meaning anywhere in a suite.
_INERT = frozenset({"EmptyLine", "Comment"})

#: Sections that are inert as a whole: comments, and the text before the first header.
_INERT_SECTIONS = frozenset({"CommentSection", "ImplicitCommentSection"})

_SUPPORTED = (
    "Supported are: Documentation and the standard Resource import in *** Settings ***; "
    "the suite variables in *** Variables ***; and exactly one test case that calls "
    f"{_RUN_NETWORK_TEST} and the criterion keywords"
)


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
    suite = _read(_parse_with_robot(source))

    missing = sorted(set(_VARIABLES) - set(suite.variables))
    if missing:
        raise ConfigError(
            "the suite is missing required variable(s): "
            + ", ".join(f"${{{name}}}" for name in missing)
        )
    if suite.test_name is None:
        raise ConfigError("the suite has no test case")
    if suite.network_test_calls != 1:
        raise ConfigError(
            f"the test case must call {_RUN_NETWORK_TEST!r} exactly once, "
            f"not {suite.network_test_calls} times"
        )

    variables = suite.variables
    return TestDefinition(
        name=suite.documentation or suite.test_name,
        source_node=variables["SOURCE_NODE"],
        target_node=variables["TARGET_NODE"],
        test_type=TestType.parse(variables["TEST_TYPE"]),
        protocol=Protocol.parse(variables["PROTOCOL"]),
        duration=Duration.parse(variables["TEST_DURATION"]),
        target_bandwidth=Bandwidth.parse(variables["TARGET_BANDWIDTH"]),
        criteria=tuple(suite.criteria),
        monitoring_mode=(
            MonitoringMode.parse(variables["MONITORING_MODE"])
            if variables.get("MONITORING_MODE")
            else MonitoringMode.AUTO
        ),
        results_directory=variables.get("RESULTS_DIRECTORY") or DEFAULT_RESULTS_DIRECTORY,
    )


@dataclass
class _Suite:
    """What a suite said, collected before the model is built from it."""

    documentation: str = ""
    variables: dict[str, str] = field(default_factory=dict)
    test_name: str | None = None
    network_test_calls: int = 0
    criteria: list[Criterion] = field(default_factory=list)


def _read(model: Any) -> _Suite:
    """Read a parsed suite, rejecting anything the renderer would not have emitted.

    Robot's :class:`~robot.api.parsing.ModelVisitor` dispatches on block type, so each
    section is handled by a method that knows what that section may hold. Within a
    section every statement is checked against an allow-list, and anything that is not
    on it — including every control structure, whose branches are then never looked
    inside at all — is a ``config_error``. Nothing can be missed by a traversal that
    forgot a child attribute, because nothing unrecognised is ever skipped.

    Nodes are typed ``Any``: this is Robot's AST, and inventing protocol types for it
    would assert a structure this code does not own.
    """
    from robot.api.parsing import ModelVisitor  # noqa: PLC0415 - see _parse_with_robot

    suite = _Suite()

    class Reader(ModelVisitor):  # type: ignore[misc]
        def visit_File(self, node: Any) -> None:  # noqa: N802 - Robot's naming
            for section in node.sections:
                kind = type(section).__name__
                if kind in _INERT_SECTIONS:
                    continue
                handler = getattr(self, f"visit_{kind}", None)
                if handler is None:
                    raise ConfigError(_unsupported(section, _section_name(section)))
                handler(section)

        def visit_SettingSection(self, node: Any) -> None:  # noqa: N802
            for statement in _body(node):
                kind = type(statement).__name__
                if kind == "Documentation":
                    suite.documentation = _documentation(statement)
                elif kind == "ResourceImport" and statement.name == RESOURCE_IMPORT:
                    continue
                else:
                    raise ConfigError(_unsupported(statement, _describe(statement)))

        def visit_VariableSection(self, node: Any) -> None:  # noqa: N802
            known = set(_VARIABLES) | set(_OPTIONAL_VARIABLES)
            for statement in _body(node):
                if type(statement).__name__ != "Variable":
                    raise ConfigError(_unsupported(statement, _describe(statement)))
                name, values = _variable(statement)
                if name not in known:
                    raise ConfigError(
                        f"line {statement.lineno}: unknown variable ${{{name}}}. Accepted "
                        "variables are: " + ", ".join(f"${{{v}}}" for v in sorted(known))
                    )
                if name in suite.variables:
                    raise ConfigError(f"line {statement.lineno}: ${{{name}}} is set twice")
                if len(values) != 1:
                    raise ConfigError(
                        f"line {statement.lineno}: ${{{name}}} must have exactly one value"
                    )
                suite.variables[name] = values[0]

        def visit_TestCaseSection(self, node: Any) -> None:  # noqa: N802
            for test in _body(node):
                if type(test).__name__ != "TestCase":
                    raise ConfigError(_unsupported(test, _describe(test)))
                if suite.test_name is not None:
                    raise ConfigError(
                        f"line {test.lineno}: in this version a suite holds exactly one test "
                        "case, one directed pair"
                    )
                suite.test_name = test.name.strip()
                for statement in _body(test):
                    _test_statement(statement, suite)

    Reader().visit(model)
    return suite


def _test_statement(statement: Any, suite: _Suite) -> None:
    """One statement inside the test case: the network test, a criterion, or an error."""
    kind = type(statement).__name__
    if kind != "KeywordCall":
        raise ConfigError(_unsupported(statement, _describe(statement)))
    if statement.assign:
        raise ConfigError(
            f"line {statement.lineno}: assigning a keyword's result is not supported "
            f"({' '.join(statement.assign)} {statement.keyword})"
        )

    keyword = statement.keyword.strip()
    normalised = _normalise(keyword)
    arguments = [str(argument) for argument in statement.args]

    if normalised == _normalise(_RUN_NETWORK_TEST):
        if set(arguments) != _RUN_NETWORK_TEST_ARGUMENTS or len(arguments) != len(
            _RUN_NETWORK_TEST_ARGUMENTS
        ):
            raise ConfigError(
                f"line {statement.lineno}: {_RUN_NETWORK_TEST} must be called with exactly "
                "these arguments, which take their values from the suite variables: "
                + "    ".join(sorted(_RUN_NETWORK_TEST_ARGUMENTS))
            )
        suite.network_test_calls += 1
        return

    subject = _CRITERION_KEYWORDS.get(normalised)
    if subject is None:
        hint = (
            " It looks like a criterion; accepted criterion keywords are: "
            + ", ".join(repr(phrase) for phrase in _criterion_phrases())
            if normalised.endswith("shouldbe")
            else ""
        )
        raise ConfigError(
            f"line {statement.lineno}: unrecognised keyword {keyword!r}. A test case may "
            f"call only {_RUN_NETWORK_TEST!r} and the criterion keywords, because anything "
            f"else would not be carried into the suite that runs.{hint}"
        )
    suite.criteria.append(_criterion(statement, keyword, subject, arguments))


def criterion_keyword_names() -> tuple[str, ...]:
    """Every criterion keyword, spelled canonically: the vocabulary a suite may use.

    The parser reads exactly these, and the Robot adapter implements exactly these, from
    this one table, so the two cannot disagree about what a criterion keyword is.
    """
    return tuple(_CRITERION_NAMES)


def criterion_from_keyword(keyword: str, arguments: Sequence[str], *, where: str = "") -> Criterion:
    """The criterion a call to *keyword* with *arguments* states.

    *where* prefixes the error messages, such as a line number.

    Raises:
        ConfigError: if *keyword* is not a criterion keyword, or its arguments do not
            make a criterion the model accepts.
    """
    subject = _CRITERION_KEYWORDS.get(_normalise(keyword))
    if subject is None:
        raise ConfigError(f"{where}{keyword!r} is not a criterion keyword")
    return _criterion_of(where, keyword, subject, list(arguments))


def _criterion(
    statement: Any,
    keyword: str,
    subject: tuple[Statistic | None, Metric],
    arguments: list[str],
) -> Criterion:
    return _criterion_of(f"line {statement.lineno}: ", keyword, subject, arguments)


def _criterion_of(
    where: str,
    keyword: str,
    subject: tuple[Statistic | None, Metric],
    arguments: list[str],
) -> Criterion:
    if len(arguments) < 3:
        raise ConfigError(
            f"{where}criterion {keyword!r} needs a comparison, a value "
            f"and a unit, got {len(arguments)} argument(s)"
        )
    statistic, metric = subject
    comparison_text, threshold_text, unit_text = arguments[0], arguments[1], " ".join(arguments[2:])
    return Criterion(
        metric=metric,
        comparison=_comparison(comparison_text, keyword),
        threshold=_threshold(threshold_text, keyword),
        unit=Unit.parse(unit_text),
        statistic=statistic,
    )


def _normalise(name: str) -> str:
    """Robot's own keyword matching: case, spaces and underscores are not significant.

    ``throughput should be``, ``Throughput_Should_Be`` and ``ThroughputShouldBe`` all
    call the same keyword in Robot, so they must all be read as the same criterion here.
    Matching more strictly than Robot does is exactly how a criterion gets dropped.
    """
    return "".join(character for character in name.casefold() if character not in " _")


def _criterion_phrases() -> list[str]:
    """Every criterion keyword, in its canonical spelling, for error messages."""
    phrases = []
    for metric in Metric:
        subject = metric.robot_phrasing
        if metric.requires_statistic:
            subject = f"<Statistic> {subject}"
        phrases.append(f"{subject} Should Be")
    return phrases


def _criterion_spellings() -> dict[str, tuple[Statistic | None, Metric]]:
    """Canonical keyword name -> (statistic, metric), for every combination.

    Including combinations the model rejects — ``Min Throughput Should Be``, a bare
    ``Latency Should Be`` — so they reach :class:`Criterion`, which says precisely why,
    instead of being reported as an unknown keyword.
    """
    spellings: dict[str, tuple[Statistic | None, Metric]] = {}
    for metric in Metric:
        spellings[f"{metric.robot_phrasing} Should Be"] = (None, metric)
        for statistic in Statistic:
            phrase = f"{statistic.robot_phrasing} {metric.robot_phrasing} Should Be"
            spellings[phrase] = (statistic, metric)
    return spellings


_CRITERION_NAMES = _criterion_spellings()
#: Normalised keyword name -> (statistic, metric), matched the way Robot matches names.
_CRITERION_KEYWORDS = {_normalise(name): subject for name, subject in _CRITERION_NAMES.items()}


def _body(node: Any) -> list[Any]:
    """A block's statements, with inert ones dropped and Robot's own errors raised."""
    statements = []
    for statement in node.body:
        kind = type(statement).__name__
        errors = getattr(statement, "errors", ()) or ()
        if errors:
            raise ConfigError(f"line {statement.lineno}: {'; '.join(errors)}")
        if kind in _INERT:
            continue
        statements.append(statement)
    return statements


def _unsupported(node: Any, what: str) -> str:
    return (
        f"line {node.lineno}: {what} is not supported. The suite that runs is re-rendered "
        f"from the parsed definition, so anything it cannot carry would silently not run. "
        f"{_SUPPORTED}"
    )


def _describe(statement: Any) -> str:
    """A reader's name for a statement: ``'IF'``, ``'[Teardown]'``, ``'Suite Setup'``.

    A block (``IF``, ``FOR``, ``TRY``...) is named by its header; a statement by its
    first data token, which is the word the user actually typed.
    """
    header = getattr(statement, "header", None)
    if header is not None:
        statement = header
    tokens = getattr(statement, "data_tokens", None) or ()
    if tokens and tokens[0].value.strip():
        return repr(tokens[0].value.strip())
    return type(statement).__name__


def _section_name(section: Any) -> str:
    header = getattr(section, "header", None)
    return repr(header.name) if header is not None and header.name else type(section).__name__


def _criterion_columns(criterion: Criterion) -> tuple[str, str, str, str]:
    """The four columns of the documented surface, unpadded.

    ``<Statistic>? <Metric> Should Be <Comparison> <Value> <Unit>``. The
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


def _comparison(text: str, keyword: str) -> Comparison:
    for comparison in Comparison:
        if _normalise(comparison.robot_phrasing) == _normalise(text):
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


def _variable(statement: Any) -> tuple[str, list[str]]:
    raw_name = (getattr(statement, "name", None) or "").strip()
    name = raw_name.removeprefix("${").removesuffix("=").strip().removesuffix("}")
    return name, [str(value) for value in getattr(statement, "value", ())]


def _documentation(statement: Any) -> str:
    value = getattr(statement, "value", "")
    if isinstance(value, str):
        return value.strip()
    return " ".join(str(part) for part in value).strip()


def _parse_with_robot(source: str) -> Any:
    """Hand the suite to Robot's own parser.

    The import lives here rather than at module scope so that importing this module,
    and therefore the core library, still works with Robot Framework absent.
    Only this function needs it; rendering does not.
    """
    try:
        from robot.api import get_model  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised by the no-Robot CI job
        raise ConfigError(
            "reading a .robot suite needs Robot Framework, which is not installed. "
            "Install dokimi with its 'robot' extra (uv tool install --python 3.14 "
            "'dokimi-io[robot]', or uv sync --extra robot in a checkout), or author the "
            "test as YAML instead — both formats parse to the same model."
        ) from exc

    # Robot's parser takes a path or a file-like object, never a bare string.
    return get_model(io.StringIO(source))
