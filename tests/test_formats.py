"""The format plugins, against the acceptance criteria on adrs#38.

The four criteria that issue names are each covered by a test below, named after the
property rather than the function, because the property is the thing that must not
regress.
"""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from dokimi.formats import (
    parse_file,
    parse_robot,
    parse_yaml,
    render_robot,
    render_yaml,
)
from dokimi.model import ConfigError, Metric, TestDefinition

YAML_SOURCE = """\
apiVersion: dokimi.io/v1alpha1
kind: NetworkTest

metadata:
  name: worker-01-to-worker-02-udp

placement:
  mode: explicit
  source: worker-01
  target: worker-02

test:
  type: iperf2
  protocol: udp
  duration: 30s
  targetBandwidth: 1G

validation:
  - metric: throughput
    comparison: atLeast
    threshold: 950
    unit: megabits per second
  - metric: packetLoss
    comparison: atMost
    threshold: 0
    unit: percent
"""


def _definition() -> TestDefinition:
    return parse_yaml(YAML_SOURCE)


# --------------------------------------------------------------------------- AC 1
@pytest.mark.requires_robot
def test_a_definition_round_trips_through_the_rendered_suite() -> None:
    """parse to model, render to .robot, re-parse, and the model is equivalent."""
    original = _definition()
    reparsed = parse_robot(render_robot(original))

    assert reparsed == original


def test_the_yaml_round_trip_needs_no_robot_framework() -> None:
    """parse -> render -> re-parse, entirely within YAML.

    Unmarked on purpose: this is the half of the round-trip property that holds with
    Robot Framework absent, so the core-without-robot job still checks something real.
    """
    original = _definition()

    assert parse_yaml(render_yaml(original)) == original


@pytest.mark.requires_robot
def test_the_round_trip_also_holds_through_yaml() -> None:
    original = _definition()

    assert parse_yaml(render_yaml(original)) == original
    assert parse_robot(render_robot(parse_yaml(render_yaml(original)))) == original


# --------------------------------------------------------------------------- AC 2
@pytest.mark.requires_robot
@pytest.mark.parametrize("metric", list(Metric))
def test_no_criterion_is_expressible_in_one_format_but_not_the_other(metric: Metric) -> None:
    """Every criterion the model allows survives both renderers and both parsers."""
    from dokimi.model import Comparison, Criterion, Statistic, Unit

    unit = {
        Metric.THROUGHPUT: Unit.MEGABITS_PER_SECOND,
        Metric.PACKET_LOSS: Unit.PERCENT,
        Metric.JITTER: Unit.MILLISECONDS,
        Metric.LATENCY: Unit.MICROSECONDS,
    }[metric]
    criterion = Criterion(
        metric=metric,
        comparison=Comparison.AT_MOST,
        threshold=Decimal("12.5"),
        unit=unit,
        statistic=Statistic.MAX if metric.requires_statistic else None,
    )
    definition = _definition()
    with_criterion = TestDefinition(
        name=definition.name,
        source_node=definition.source_node,
        target_node=definition.target_node,
        test_type=definition.test_type,
        protocol=definition.protocol,
        duration=definition.duration,
        target_bandwidth=definition.target_bandwidth,
        criteria=(criterion,),
    )

    assert parse_robot(render_robot(with_criterion)).criteria == (criterion,)
    assert parse_yaml(render_yaml(with_criterion)).criteria == (criterion,)


# --------------------------------------------------------------------------- AC 3
def test_the_core_library_imports_without_robot_and_only_the_robot_parser_fails() -> None:
    """Importing the library with Robot absent works; parse_robot fails clearly.

    Run in a subprocess with Robot hidden from the import system, because the test
    session itself has Robot installed.
    """
    probe = """
import sys

class Blocker:
    # find_spec, not the legacy find_module: that was removed in Python 3.12, so a
    # blocker using it silently does nothing and the test passes without testing.
    def find_spec(self, name, path=None, target=None):
        if name == "robot" or name.startswith("robot."):
            raise ImportError("Robot Framework is hidden for this test")
        return None

sys.meta_path.insert(0, Blocker())
for name in [n for n in sys.modules if n == "robot" or n.startswith("robot.")]:
    del sys.modules[name]

import dokimi
import dokimi.formats as formats

# Rendering must work with no Robot Framework anywhere.
from dokimi.formats import render_robot, parse_yaml
suite = render_robot(parse_yaml(open(sys.argv[1], encoding="utf-8").read()))
assert "*** Test Cases ***" in suite

# Parsing must fail, and say what to do about it.
try:
    formats.parse_robot(suite)
except Exception as exc:
    assert "Robot Framework" in str(exc), str(exc)
    assert "extra" in str(exc) or "YAML" in str(exc), str(exc)
    print("OK")
else:
    raise AssertionError("parse_robot succeeded with Robot Framework hidden")
"""
    document = Path(__file__).parent / "_roundtrip.yaml"
    document.write_text(YAML_SOURCE, encoding="utf-8")
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe, str(document)],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        document.unlink(missing_ok=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


# --------------------------------------------------------------------------- AC 4
@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("  targetBandwith: 1G\n", "unknown field"),
        ("", "missing required field"),
    ],
)
def test_malformed_input_is_a_config_error(mutation: str, expected: str) -> None:
    """A typo is rejected, not ignored. No cluster interaction is possible from here."""
    broken = YAML_SOURCE.replace("  targetBandwidth: 1G\n", mutation)

    with pytest.raises(ConfigError, match=expected):
        parse_yaml(broken)


def test_an_unknown_apiversion_is_rejected() -> None:
    with pytest.raises(ConfigError, match="unsupported apiVersion"):
        parse_yaml(YAML_SOURCE.replace("v1alpha1", "v1"))


def test_a_selector_placement_mode_is_out_of_milestone_01_scope() -> None:
    # ADR-04-06 scopes Milestone-01 to two explicitly named nodes.
    with pytest.raises(ConfigError, match="Milestone-01"):
        parse_yaml(YAML_SOURCE.replace("mode: explicit", "mode: allToAll"))


def test_a_definition_with_no_criteria_is_rejected() -> None:
    # The YAML plugin catches this first, with a message about the field it read.
    without = YAML_SOURCE.split("validation:")[0] + "validation: []\n"

    with pytest.raises(ConfigError, match="non-empty list of criteria"):
        parse_yaml(without)


def test_the_model_itself_refuses_a_definition_with_no_criteria() -> None:
    # The same rule one layer down, so it holds for any format, not just YAML:
    # a run with no criteria has nothing to report a verdict against.
    definition = _definition()

    with pytest.raises(ConfigError, match="at least one pass criterion"):
        TestDefinition(
            name=definition.name,
            source_node=definition.source_node,
            target_node=definition.target_node,
            test_type=definition.test_type,
            protocol=definition.protocol,
            duration=definition.duration,
            target_bandwidth=definition.target_bandwidth,
            criteria=(),
        )


# ------------------------------------------------------------------- the renderer
def test_the_rendered_suite_matches_the_documented_keyword_surface() -> None:
    """`<Statistic>? <Metric> Should Be <Comparison> <Value> <Unit>`.

    Whitespace is collapsed before comparing: the column alignment is cosmetic, and
    pinning it would make every future formatting tweak look like a broken contract.
    """
    lines = [" ".join(line.split()) for line in render_robot(_definition()).splitlines()]

    assert "Throughput Should Be At Least 950 megabits per second" in lines
    assert "Packet Loss Should Be At Most 0 percent" in lines
    assert "Resource resources/Dokimi.resource" in lines


def test_the_rendered_criterion_columns_line_up() -> None:
    # Cosmetic, but the suite is meant to be read by a person, and the README shows it
    # aligned. Checked separately from the grammar so a formatting change breaks only
    # this test.
    criteria = [line for line in render_robot(_definition()).splitlines() if "Should Be" in line]

    assert len(criteria) == 2
    assert len({line.index("At ") for line in criteria}) == 1


@pytest.mark.requires_robot
def test_parse_file_chooses_the_format_from_the_suffix(tmp_path: Path) -> None:
    document = tmp_path / "pair.yaml"
    document.write_text(YAML_SOURCE, encoding="utf-8")
    suite = tmp_path / "pair.robot"
    suite.write_text(render_robot(_definition()), encoding="utf-8")

    assert parse_file(document) == parse_file(suite)


def test_an_unrecognised_suffix_is_a_config_error(tmp_path: Path) -> None:
    document = tmp_path / "pair.json"
    document.write_text("{}", encoding="utf-8")

    with pytest.raises(ConfigError, match="cannot tell which format"):
        parse_file(document)


# ----------------------------------------------------- YAML hardening (review of #26)
def test_a_name_yaml_would_misread_is_quoted_on_the_way_out() -> None:
    # `name: a: b` is not valid YAML. The emitter quotes it; the f-string did not.
    from dataclasses import replace

    original = replace(_definition(), name="a: b")

    assert parse_yaml(render_yaml(original)) == original


@pytest.mark.parametrize("threshold", [".nan", ".inf", "-.inf"])
def test_a_non_finite_yaml_threshold_is_a_config_error(threshold: str) -> None:
    source = YAML_SOURCE.replace("threshold: 950", f"threshold: {threshold}")

    with pytest.raises(ConfigError, match="must be a finite number"):
        parse_yaml(source)


def test_the_rendered_yaml_keeps_the_documented_layout() -> None:
    # Emitting through yaml.dump must not have changed what a person reads.
    rendered = render_yaml(_definition())

    assert "\nmetadata:\n  name: worker-01-to-worker-02-udp\n" in rendered
    assert "  - metric: throughput\n    comparison: atLeast\n    threshold: 950\n" in rendered
    assert "\n\nvalidation:\n" in rendered
