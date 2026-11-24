"""The shipped examples must parse, and must agree with each other.

Documentation that the parser rejects is worse than no documentation, and the two formats
are peers, so an example that reads differently in each is exactly the drift that must not
happen. Both are cheap to check, so they are checked on every run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dokimi.formats import SUFFIXES, parse, parse_file
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


# -- the documentation's own examples -------------------------------------------
#
# The README's Robot example was written before any parser existed and never parsed:
# it hard-coded `type=iperf2` and left out ${TEST_TYPE} and ${PROTOCOL}. examples/
# was created to carry the README's examples so they could be exercised, and the YAML
# one stayed identical — but the Robot one was fixed in examples/ only, and nothing
# compared the two. These tests are what compares them.

REPOSITORY = EXAMPLES.parent
DOCUMENTS = [
    REPOSITORY / "README.md",
    EXAMPLES / "README.md",
    *sorted((REPOSITORY / "docs").glob("*.md")),
]
_FENCED = re.compile(r"```[a-z]*\n(.*?)```", re.S)


def _authoring_blocks() -> list[tuple[str, str, str]]:
    """Every fenced block in the docs that is a test definition: (where, format, text)."""
    found = []
    for document in DOCUMENTS:
        text = document.read_text()
        for match in _FENCED.finditer(text):
            body = match.group(1)
            fmt = (
                "robot"
                if "*** Test Cases ***" in body
                else "yaml"
                if "kind: NetworkTest" in body
                else None
            )
            if fmt is not None:
                line = text.count("\n", 0, match.start()) + 1
                found.append((f"{document.relative_to(REPOSITORY)}:{line}", fmt, body))
    return found


@pytest.mark.parametrize(
    ("where", "fmt", "body"),
    _authoring_blocks(),
    ids=[where for where, _, _ in _authoring_blocks()],
)
def test_every_test_definition_in_the_docs_parses(where: str, fmt: str, body: str) -> None:
    # Documentation that the parser rejects is worse than none: it is copied, and fails.
    if fmt == "robot" and not robot_framework_installed():
        pytest.skip("needs the robot extra to read a .robot suite")

    parse(body, fmt)


@pytest.mark.parametrize("path", _example_files(), ids=lambda p: p.name)
def test_the_readme_shows_the_shipped_examples_verbatim(path: Path) -> None:
    # Verbatim, not merely equivalent: the point of examples/ is that the README's
    # examples are real files the suite exercises, so any difference is drift.
    blocks = [match.group(1) for match in _FENCED.finditer((REPOSITORY / "README.md").read_text())]

    assert path.read_text() in blocks, f"README.md no longer shows {path.name} as it is"
