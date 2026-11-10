"""Every GitHub Action the workflows use is pinned to a commit, and every runner to a release.

A tag like `actions/checkout@v4` is a moving pointer: whoever controls the action's
repository can point it at different code, and every workflow that names it runs that code
on its next run, with that workflow's permissions. The release workflow holds
`packages: write` and `id-token: write`, so that is not an abstract risk. A commit SHA
cannot move. The version stays in a trailing comment so a reader, and a dependency bot,
can still tell what it is.

`ubuntu-latest` moves too. When GitHub moved it from 24.04 to 26.04, the shells the
completion tests run in changed version under a green branch. A runner named by its release
changes only when someone changes it, in a pull request of its own.
"""

from __future__ import annotations

import pathlib
import re

import pytest

WORKFLOWS = sorted(
    (pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows").glob("*.yml")
)
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)(?P<rest>.*)$", re.M)
RUNS_ON = re.compile(r"^\s*runs-on:\s*(?P<runner>\S+)", re.M)


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit(workflow: pathlib.Path) -> None:
    for use in USES.finditer(workflow.read_text()):
        ref = use["ref"]
        if ref.startswith("./"):
            continue  # a local action is part of this repository's own history
        action, _, version = ref.partition("@")
        assert re.fullmatch(r"[0-9a-f]{40}", version), f"{workflow.name}: {ref} is not pinned"
        assert re.search(r"#\s*v\d+\.\d+\.\d+", use["rest"]), (
            f"{workflow.name}: {action} is pinned but says no version"
        )


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_every_runner_is_named_by_its_release(workflow: pathlib.Path) -> None:
    runners = [match["runner"] for match in RUNS_ON.finditer(workflow.read_text())]

    assert runners, f"{workflow.name}: no runs-on"
    for runner in runners:
        assert not runner.endswith("-latest"), f"{workflow.name}: {runner} moves"


def test_there_are_workflows_to_check() -> None:
    assert {p.name for p in WORKFLOWS} >= {"ci.yml", "release.yml", "test-env.yml"}
