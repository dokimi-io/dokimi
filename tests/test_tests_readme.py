"""The test suite's README lists every test file, so the table of what is tested where can't drift.

The README used to carry each file's test count as well. Those changed with nearly every pull
request and conflicted in all of them, so the table keeps only what each file tests, and this
checks that no file is missing from it or listed after it is gone.
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent
README = TESTS / "README.md"


def listed() -> set[str]:
    return set(re.findall(r"^\| \[`(test_\w+\.py)`\]\(", README.read_text(encoding="utf-8"), re.M))


def test_every_test_file_has_a_row() -> None:
    files = {path.name for path in TESTS.glob("test_*.py")}

    assert sorted(files - listed()) == []


def test_every_row_names_a_test_file_that_exists() -> None:
    files = {path.name for path in TESTS.glob("test_*.py")}

    assert sorted(listed() - files) == []
