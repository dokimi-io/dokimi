"""M1-17: the end-to-end acceptance run, against a live cluster.

Every scenario submits a real run through the CLI, exactly as a user would, and checks the
three things no unit test can: the exit code the run came to, what reached the submitter's
results directory, and what the run left in the cluster. Nothing here is faked. It is where
the pieces the test suite checks one at a time have to work together.

Run it against a cluster with dokimi's RBAC applied and at least two schedulable workers,
with the environment ``test-env.sh images`` prints, the images and the cluster's name:

    test-env/test-env.sh up kind-kindnet
    out=$(test-env/test-env.sh images) && eval "$out"
    kubectl apply -f deploy/rbac.yaml
    uv run python test-env/acceptance.py

It prints one line per check and exits 0 only if every check passed. Every run it starts
is removed again at the end, its Prometheus included.

The criteria are chosen for what they exercise, never borrowed from a realistic target: an
emulated cluster says nothing about network performance, so a passing run asks for very
little.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dokimi.k8s.identity import COMPONENT_LABEL, MANAGED_BY, MANAGED_BY_LABEL, RUN_ID_LABEL
from dokimi.k8s.ownership import PROMETHEUS_COMPONENT

#: The namespace the iperf2 test type runs in, as the CLI derives it.
NAMESPACE = "dokimi-iperf2"
SERVICE_ACCOUNT = "dokimi"
#: A test pair's UDP target, and how long it sends: little and short, since only the
#: orchestration is under test.
BANDWIDTH = "10M"
DURATION = "10s"
#: What every run must bring back, besides Robot's per-pair files.
ARTIFACTS = (
    "output.xml",
    "log.html",
    "report.html",
    "tests.csv",
    "tests.jsonl",
    "assertions.csv",
    "assertions.jsonl",
    "run.json",
    "diagnostics.json",
)
#: How long a deleted Job's pods may take to go after the CLI has removed the Job.
SETTLE_SECONDS = 90

_SUBMITTED = re.compile(r"submitted run (?P<run_id>\d{8}-\d{6}-[0-9a-z]{6})")


@dataclass(frozen=True)
class Criterion:
    metric: str
    comparison: str
    threshold: float
    unit: str


#: The passing run's criteria: both kinds the milestone evaluates, minimum throughput and
#: maximum packet loss, set so low that any cluster that runs the test at all meets them.
PASSING = (
    Criterion("throughput", "atLeast", 1, "megabits per second"),
    Criterion("packetLoss", "atMost", 10, "percent"),
)


def definition(name: str, source: str, target: str, criteria: Sequence[Criterion]) -> str:
    """A YAML test definition for one directed UDP pair, with its results in ``./results``."""
    validation = "".join(
        f"  - metric: {c.metric}\n"
        f"    comparison: {c.comparison}\n"
        f"    threshold: {c.threshold:g}\n"
        f"    unit: {c.unit}\n"
        for c in criteria
    )
    return (
        "apiVersion: dokimi.io/v1alpha1\n"
        "kind: NetworkTest\n"
        "metadata:\n"
        f"  name: {name}\n"
        "placement:\n"
        "  mode: explicit\n"
        f"  source: {source}\n"
        f"  target: {target}\n"
        "test:\n"
        "  type: iperf2\n"
        "  protocol: udp\n"
        f"  duration: {DURATION}\n"
        f"  targetBandwidth: {BANDWIDTH}\n"
        "monitoring:\n"
        "  mode: managed\n"
        f"validation:\n{validation}"
        "results:\n"
        "  directory: ./results\n"
    )


def schedulable_workers(nodes: Mapping[str, Any]) -> list[str]:
    """The nodes a test pair can run on: Ready, not cordoned, with no taint that repels.

    *nodes* is ``kubectl get nodes -o json``. Sorted by name, so a run is repeatable.
    """
    workers = []
    for node in nodes.get("items", []):
        spec = node.get("spec") or {}
        if spec.get("unschedulable"):
            continue
        if any(t.get("effect") in ("NoSchedule", "NoExecute") for t in spec.get("taints") or []):
            continue
        conditions = (node.get("status") or {}).get("conditions") or []
        if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
            workers.append(str(node["metadata"]["name"]))
    return sorted(workers)


@dataclass
class Result:
    """One CLI invocation: what it exited with and printed, and the run it started, if any."""

    exit_code: int
    stdout: str
    stderr: str
    run_id: str | None


@dataclass
class Acceptance:
    """The checks so far, the runs to remove at the end, and how to reach the cluster."""

    work: Path
    source: str
    target: str
    failures: list[str] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)

    def check(self, condition: bool, what: str) -> bool:
        print(f"  {'ok  ' if condition else 'FAIL'}  {what}", flush=True)
        if not condition:
            self.failures.append(what)
        return condition

    def dokimi(self, *args: str, cwd: Path | None = None) -> Result:
        completed = subprocess.run(
            [sys.executable, "-m", "dokimi.cli.main", *args],
            cwd=cwd or self.work,
            capture_output=True,
            text=True,
            check=False,
        )
        match = _SUBMITTED.search(completed.stderr)
        run_id = match["run_id"] if match else None
        if run_id is not None:
            self.runs.append(run_id)
        return Result(completed.returncode, completed.stdout, completed.stderr, run_id)

    def submit(
        self, name: str, criteria: Sequence[Criterion], *, target: str | None = None
    ) -> Result:
        """Write a definition into a directory of its own and submit it, following the run."""
        directory = self.work / name
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "test.yaml"
        path.write_text(definition(name, self.source, target or self.target, criteria))
        return self.dokimi("submit", str(path), "--service-account", SERVICE_ACCOUNT, cwd=directory)

    def run_owned(self, run_id: str) -> list[dict[str, Any]]:
        """Every object in the cluster that carries *run_id*'s label."""
        return _kubectl_items("all,configmaps", f"{RUN_ID_LABEL}={run_id}")

    def runs_in_cluster(self) -> set[str]:
        """The run ids any dokimi-managed owner or Job in the cluster carries."""
        items = _kubectl_items("configmaps,jobs", f"{MANAGED_BY_LABEL}={MANAGED_BY}")
        return {
            str(i["metadata"]["labels"][RUN_ID_LABEL])
            for i in items
            if _labels(i).get(RUN_ID_LABEL)
        }


def _labels(item: Mapping[str, Any]) -> dict[str, str]:
    return dict((item.get("metadata") or {}).get("labels") or {})


def _kubectl_items(kinds: str, selector: str) -> list[dict[str, Any]]:
    completed = subprocess.run(
        ["kubectl", "get", kinds, "--all-namespaces", "-l", selector, "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    items: list[dict[str, Any]] = json.loads(completed.stdout).get("items", [])
    return items


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _describe(items: Iterable[Mapping[str, Any]]) -> str:
    return ", ".join(f"{i['kind']}/{i['metadata']['name']}" for i in items) or "nothing"


# --- scenarios ---------------------------------------------------------------------------


def happy_path(a: Acceptance) -> None:
    """A run that passes: exit 0, every report back, and only its Prometheus left behind."""
    result = a.submit("happy-path", PASSING)
    a.check(result.run_id is not None, "the run was submitted")
    if not a.check(result.exit_code == 0, f"it exits 0 (got {result.exit_code})"):
        print(result.stderr[-3000:])
    if result.run_id is None:
        return
    reports = a.work / "happy-path" / "results" / result.run_id
    for name in ARTIFACTS:
        a.check((reports / name).is_file(), f"{name} reached results/<run-id>/")
    if (reports / "tests.jsonl").is_file():
        tests = _jsonl(reports / "tests.jsonl")
        a.check(
            [t["outcome"] for t in tests] == ["pass"],
            f"tests.jsonl holds one test, passed ({[t['outcome'] for t in tests]})",
        )
        a.check(
            all((t.get("throughput_bits_per_second") or 0) > 0 for t in tests),
            "with a throughput measured",
        )
    if (reports / "assertions.jsonl").is_file():
        outcomes = [r["outcome"] for r in _jsonl(reports / "assertions.jsonl")]
        a.check(
            outcomes == ["pass", "pass"],
            f"assertions.jsonl holds both criteria, passed ({outcomes})",
        )
    if (reports / "run.json").is_file():
        record = json.loads((reports / "run.json").read_text(encoding="utf-8"))
        a.check(
            (record.get("outcome"), record.get("exit_code")) == ("pass", 0),
            f"run.json says pass and 0 ({record.get('outcome')}, {record.get('exit_code')})",
        )
        a.check(
            (record.get("cleanup") or {}).get("complete") is True,
            "run.json says its cleanup was complete",
        )

    # The CLI removes the Job once it has the reports; its pods go a moment later.
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        left = [
            i
            for i in a.run_owned(result.run_id)
            if _labels(i).get(COMPONENT_LABEL) != PROMETHEUS_COMPONENT
        ]
        if not left or time.monotonic() > deadline:
            break
        time.sleep(3)
    a.check(not left, f"only its Prometheus is left in the cluster (also: {_describe(left)})")


def unknown_node(a: Acceptance) -> None:
    """A node the cluster doesn't have: a config error, exit 3, and nothing created."""
    before = a.runs_in_cluster()
    result = a.submit("unknown-node", PASSING, target="no-such-node")
    a.check(result.exit_code == 3, f"it exits 3 (got {result.exit_code})")
    a.check("no-such-node" in result.stderr, "the error names the node")
    a.check(result.run_id is None, "no run was submitted")
    created = a.runs_in_cluster() - before
    a.check(not created, f"nothing was created in the cluster ({sorted(created) or 'nothing'})")


SCENARIOS: dict[str, Callable[[Acceptance], None]] = {
    "happy-path": happy_path,
    "unknown-node": unknown_node,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("scenarios", nargs="*", help=f"any of {', '.join(SCENARIOS)}; all if none")
    parser.add_argument("--source", help="the source node, instead of the first schedulable worker")
    parser.add_argument("--target", help="the target node, instead of the second")
    parser.add_argument("--keep", action="store_true", help="leave the runs in the cluster")
    args = parser.parse_args(argv)
    if unknown := [name for name in args.scenarios if name not in SCENARIOS]:
        parser.error(f"no such scenario: {', '.join(unknown)}")

    nodes = json.loads(
        subprocess.run(
            ["kubectl", "get", "nodes", "-o", "json"], capture_output=True, text=True, check=True
        ).stdout
    )
    workers = schedulable_workers(nodes)
    source = args.source or (workers[0] if workers else "")
    target = args.target or (workers[1] if len(workers) > 1 else "")
    if not source or not target:
        print(f"acceptance: needs two schedulable workers, found {workers}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="dokimi-acceptance-") as work:
        acceptance = Acceptance(Path(work), source, target)
        print(f"acceptance: {source} -> {target}, reports under {work}")
        try:
            for name in args.scenarios or SCENARIOS:
                print(f"\n{name}", flush=True)
                SCENARIOS[name](acceptance)
        finally:
            if not args.keep:
                print("\nremoving the runs", flush=True)
                for run_id in acceptance.runs:
                    removed = acceptance.dokimi("cleanup", run_id, "--prometheus")
                    acceptance.check(
                        removed.exit_code == 0, f"dokimi cleanup {run_id} --prometheus exits 0"
                    )

    if acceptance.failures:
        print(f"\nacceptance: {len(acceptance.failures)} check(s) failed")
        return 1
    print("\nacceptance: every check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
