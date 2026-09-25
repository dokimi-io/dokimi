"""The shipped examples must parse, and must agree with each other.

Documentation that the parser rejects is worse than no documentation, and an example in
two formats that quietly drift apart is exactly what ADR-04-05's peer-format rule exists
to prevent. Both are cheap to check, so they are checked on every run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_orchestrator.formats import SUFFIXES, parse_file
from tests.conftest import robot_framework_installed

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _example_files() -> list[Path]:
    return sorted(p for p in EXAMPLES.iterdir() if p.suffix.lower() in SUFFIXES)


def test_there_are_examples_in_both_formats() -> None:
    suffixes = {p.suffix.lower() for p in _example_files()}

    assert ".yaml" in suffixes
    assert ".robot" in suffixes


@pytest.mark.parametrize("path", _example_files(), ids=lambda p: p.name)
def test_every_shipped_example_parses(path: Path) -> None:
    if path.suffix.lower() == ".robot" and not robot_framework_installed():
        pytest.skip("needs the robot extra to read a .robot suite")

    parse_file(path)


@pytest.mark.requires_robot
def test_the_two_formats_describe_the_same_test() -> None:
    by_suffix = {p.suffix.lower(): parse_file(p) for p in _example_files()}

    assert by_suffix[".yaml"] == by_suffix[".robot"]
