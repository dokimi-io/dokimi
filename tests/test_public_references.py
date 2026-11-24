"""Nothing in the repository points into the private decision records.

The maintainers record every architectural decision, but the records are not public, so an id
or a link into them is one no reader of this repository can follow. The code and the docs
carry the reasoning in their own words instead (CONTRIBUTING.md, "How decisions are made").
Issues and pull requests may still name a record; they are not part of the repository.

This checks every file git tracks, so a reference can't come back in a file nobody thought of.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parent.parent

# Spelled in pieces where the literal would match itself.
_RECORDS = "dokimi" + "-adrs"
FORBIDDEN = {
    "a decision record's id": re.compile(r"\bADR-\d{2}-\d{2}\b"),
    "an open question's id": re.compile(r"\bOQ-\d+\b"),
    "the decision repository": re.compile(re.escape(_RECORDS)),
    "an issue or pull request in the decision repository": re.compile(r"\badrs#\d+"),
}


def _tracked_files() -> list[Path]:
    if shutil.which("git") is None or not (REPOSITORY / ".git").exists():
        pytest.skip("needs a git checkout to know which files are the repository's")
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPOSITORY,
        capture_output=True,
        check=True,
    ).stdout.decode()
    return [REPOSITORY / name for name in listed.split("\0") if name]


def _references(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError, FileNotFoundError:
        return []  # binary, or deleted but not yet staged
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        for what, pattern in FORBIDDEN.items():
            for match in pattern.finditer(line):
                where = path.relative_to(REPOSITORY)
                found.append(f"{where}:{number}: {match.group()} ({what})")
    return found


def test_the_patterns_find_what_they_are_for() -> None:
    # A pattern that matches nothing would pass every repository, this one included.
    samples = {
        "a decision record's id": "as ADR-05-04 decides",
        "an open question's id": "see OQ-137",
        "the decision repository": f"https://github.com/dokimi-io/{_RECORDS}/blob/main/x.md",
        "an issue or pull request in the decision repository": "M1-09 (adrs#44)",
    }

    for what, sample in samples.items():
        assert FORBIDDEN[what].search(sample), what


def test_ordinary_words_are_not_references() -> None:
    harmless = "an ADR, an open question, adrs in general, ADR-like, OQ, version 0.1-05-04"

    assert not any(pattern.search(harmless) for pattern in FORBIDDEN.values())


def test_no_tracked_file_points_into_the_decision_records() -> None:
    found = [reference for path in _tracked_files() for reference in _references(path)]

    assert found == [], (
        "The decision records are private, so say the reason in your own words instead:\n"
        + "\n".join(found)
    )
