"""The Robot parser reads every criterion, or refuses the suite. Nothing in between.

The suite that executes is never the one the user wrote: it is ``render_robot()`` of
the parsed model. So a construct the parser steps over is a construct that
silently never runs — and when that construct is a criterion, the run passes without
checking it. That is the false PASS raised in review of #26, and every test here is one
way it could happen.

Two directions, both required:

* **What Robot would call, the parser must read.** Robot matches keyword names ignoring
  case, spaces and underscores, so the parser has to as well. Matching more strictly than
  Robot is exactly how a valid criterion was being dropped.
* **What the renderer would not emit, the parser must reject.** Control structures,
  settings that run keywords, extra sections, stray variables, literal arguments.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dokimi.formats import parse_robot, render_robot
from dokimi.model import ConfigError

pytestmark = pytest.mark.requires_robot

SUITE = (
    Path(__file__).resolve().parent.parent / "examples" / "worker-01-to-worker-02-udp.robot"
).read_text()
LOSS = "    Packet Loss Should Be    At Most     0      percent"


def _metrics(source: str) -> list[str]:
    return [criterion.metric.value for criterion in parse_robot(source).criteria]


def test_the_example_reads_both_criteria() -> None:
    assert _metrics(SUITE) == ["throughput", "packetLoss"]


# -- what Robot would call, the parser must read ------------------------------


@pytest.mark.parametrize(
    "spelling",
    [
        "packet loss should be",
        "PACKET LOSS SHOULD BE",
        "Packet_Loss_Should_Be",
        "PacketLossShouldBe",
        "packet  loss should be".replace("  ", " "),
    ],
)
def test_a_criterion_is_read_however_robot_would_spell_it(spelling: str) -> None:
    # The regression at the heart of the review: this line was valid Robot, Robot would
    # have called the keyword, and the parser silently dropped it.
    source = SUITE.replace("Packet Loss Should Be", spelling)

    assert _metrics(source) == ["throughput", "packetLoss"]


def test_the_comparison_is_matched_the_same_way() -> None:
    source = SUITE.replace("At Most     0", "at_most     0")

    assert parse_robot(source).criteria[1].comparison.value == "atMost"


def test_run_network_test_is_matched_the_same_way() -> None:
    assert _metrics(SUITE.replace("Run Network Test", "run network test"))


def test_the_rendered_suite_always_uses_the_canonical_spelling() -> None:
    # Lenient on the way in, canonical on the way out: what executes is uniform.
    rendered = render_robot(
        parse_robot(SUITE.replace("Packet Loss Should Be", "packet loss should be"))
    )

    assert "Packet Loss Should Be" in rendered
    assert "packet loss should be" not in rendered


# -- what the renderer would not emit, the parser must reject -----------------

_IF_ELSE = "    IF    $TRUE\n        Log    x\n    ELSE\n    " + LOSS + "\n    END"
_IF_FALSE = "    IF    $FALSE\n    " + LOSS + "\n    END"
_FOR = "    FOR    ${i}    IN RANGE    1\n    " + LOSS + "\n    END"
_TRY = "    TRY\n    " + LOSS + "\n    EXCEPT\n        Log    x\n    END"
_WHILE = "    WHILE    $FALSE\n    " + LOSS + "\n    END"


@pytest.mark.parametrize(
    ("replacement", "expected"),
    [
        pytest.param(_IF_ELSE, "'IF' is not supported", id="criterion in ELSE"),
        pytest.param(_IF_FALSE, "'IF' is not supported", id="criterion in IF"),
        pytest.param(_FOR, "'FOR' is not supported", id="criterion in FOR"),
        pytest.param(_TRY, "'TRY' is not supported", id="criterion in TRY"),
        pytest.param(_WHILE, "'WHILE' is not supported", id="criterion in WHILE"),
        pytest.param(
            "    [Teardown]    Packet Loss Should Be    At Most    0    percent",
            "'[Teardown]' is not supported",
            id="criterion in [Teardown]",
        ),
        pytest.param(
            "    Packet Loss Shuld Be    At Most     0      percent",
            "unrecognised keyword 'Packet Loss Shuld Be'",
            id="typo in a criterion",
        ),
        pytest.param(
            "    Log    this would be dropped",
            "unrecognised keyword 'Log'",
            id="any other keyword",
        ),
        pytest.param(
            "    ${loss}=    Packet Loss Should Be    At Most    0    percent",
            "assigning a keyword's result",
            id="assignment",
        ),
        pytest.param(
            LOSS
            + "\n    Run Network Test    type=${TEST_TYPE}    protocol=${PROTOCOL}"
            + "    source=${SOURCE_NODE}    target=${TARGET_NODE}"
            + "    bandwidth=${TARGET_BANDWIDTH}    duration=${TEST_DURATION}",
            "must call 'Run Network Test' exactly once",
            id="network test twice",
        ),
        pytest.param(
            "    [Bogus]    x",
            "Non-existing setting 'Bogus'",
            id="Robot's own syntax error",
        ),
    ],
)
def test_a_construct_the_renderer_would_not_emit_is_rejected(
    replacement: str, expected: str
) -> None:
    with pytest.raises(ConfigError, match=re.escape(expected)):
        parse_robot(SUITE.replace(LOSS, replacement))


def test_a_typo_ending_in_should_be_lists_the_real_keywords() -> None:
    with pytest.raises(ConfigError, match="accepted criterion keywords are"):
        parse_robot(SUITE.replace("Packet Loss Should Be", "Packet Lost Should Be"))


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        pytest.param(
            lambda s: s.replace("Resource ", "Suite Teardown    Log    x\nResource "),
            "'Suite Teardown' is not supported",
            id="suite teardown",
        ),
        pytest.param(
            lambda s: s.replace("Resource ", "Library    Collections\nResource "),
            "'Library' is not supported",
            id="library import",
        ),
        pytest.param(
            lambda s: s.replace("resources/Dokimi.resource", "resources/Something.resource"),
            "'Resource' is not supported",
            id="a different resource",
        ),
        pytest.param(
            lambda s: s + "\n*** Keywords ***\nThroughput Should Be\n    Log    fake\n",
            "'Keywords' is not supported",
            id="a keyword that shadows a criterion",
        ),
        pytest.param(
            lambda s: s + "\nAnother Test\n    Run Network Test\n",
            "exactly one test case",
            id="second test case",
        ),
        pytest.param(
            lambda s: s.replace("${PROTOCOL}", "${WARMUP}            5s\n${PROTOCOL}", 1),
            "unknown variable",
            id="stray variable",
        ),
        pytest.param(
            lambda s: s.replace(
                "${PROTOCOL}            udp", "${PROTOCOL}    udp\n${PROTOCOL}    tcp"
            ),
            "set twice",
            id="variable set twice",
        ),
        pytest.param(
            lambda s: s.replace("source=${SOURCE_NODE}", "source=worker-09"),
            "must be called with exactly these arguments",
            id="literal argument overridden by the variable",
        ),
    ],
)
def test_a_suite_level_construct_the_renderer_would_not_emit_is_rejected(
    mutate: object, expected: str
) -> None:
    source = mutate(SUITE)  # type: ignore[operator]
    with pytest.raises(ConfigError, match=expected):
        parse_robot(source)


def test_comments_and_blank_lines_are_fine() -> None:
    # Inert everywhere, and the renderer's own output has blank lines.
    commented = "# a comment before any section\n" + SUITE.replace(
        LOSS, "    # a comment in the test case\n" + LOSS
    )

    assert _metrics(commented) == ["throughput", "packetLoss"]


def test_errors_point_at_the_line() -> None:
    with pytest.raises(ConfigError, match=r"^line \d+: "):
        parse_robot(SUITE.replace(LOSS, "    Log    x"))
