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
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dokimi.k8s.identity import COMPONENT_LABEL, RUN_ID_LABEL
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
    statistic: str | None = None


#: The passing run's criteria: both kinds the milestone evaluates, minimum throughput and
#: maximum packet loss, set so low that any cluster that runs the test at all meets them.
PASSING = (
    Criterion("throughput", "atLeast", 1, "megabits per second"),
    Criterion("packetLoss", "atMost", 10, "percent"),
)
#: A criterion no emulated cluster meets, so the run measures and then fails.
FAILING = (Criterion("throughput", "atLeast", 100, "gigabits per second"),)
#: A criterion on a metric iperf2 never produces: no data, so an error, never a pass. It
#: stands in for an exporter that serves no result, which only unit tests cover so far.
NEVER_MEASURED = (Criterion("latency", "atMost", 1, "milliseconds", statistic="avg"),)

#: Denies ingress to the test's iperf2 servers only. The client's trigger, the run's Job
#: and its Prometheus are untouched, so the preflight fails at the one check this is for:
#: the probe from client to server can't connect.
ISOLATE_SERVERS = f"""\
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: dokimi-acceptance-isolate-servers
  namespace: {NAMESPACE}
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: iperf2
      {COMPONENT_LABEL}: server
  policyTypes: [Ingress]
"""


def definition(name: str, source: str, target: str, criteria: Sequence[Criterion]) -> str:
    """A YAML test definition for one directed UDP pair, with its results in ``./results``."""
    validation = "".join(
        f"  - metric: {c.metric}\n"
        + (f"    statistic: {c.statistic}\n" if c.statistic else "")
        + f"    comparison: {c.comparison}\n"
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
        return self._result(completed.returncode, completed.stdout, completed.stderr)

    def started(self, *args: str, cwd: Path) -> subprocess.Popen[str]:
        """The CLI, started and not waited for, so that two can run at the same moment."""
        return subprocess.Popen(
            [sys.executable, "-m", "dokimi.cli.main", *args],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def finished(self, process: subprocess.Popen[str]) -> Result:
        stdout, stderr = process.communicate()
        return self._result(process.returncode, stdout, stderr)

    def _result(self, exit_code: int, stdout: str, stderr: str) -> Result:
        match = _SUBMITTED.search(stderr)
        run_id = match["run_id"] if match else None
        if run_id is not None:
            self.runs.append(run_id)
        return Result(exit_code, stdout, stderr, run_id)

    def write(self, name: str, criteria: Sequence[Criterion], *, target: str | None = None) -> Path:
        """Write a definition into a directory of its own. Returns the definition's path."""
        directory = self.work / name
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "test.yaml"
        path.write_text(definition(name, self.source, target or self.target, criteria))
        return path

    def submit(
        self, name: str, criteria: Sequence[Criterion], *, target: str | None = None
    ) -> Result:
        """Write a definition and submit it, following the run to its end."""
        path = self.write(name, criteria, target=target)
        return self.dokimi(
            "submit", str(path), "--service-account", SERVICE_ACCOUNT, cwd=path.parent
        )

    def reports(self, name: str, run_id: str) -> Path:
        return self.work / name / "results" / run_id

    def run_owned(self, run_id: str) -> list[dict[str, Any]]:
        """Every object in the cluster that carries *run_id*'s label."""
        return _kubectl_items("all,configmaps", f"{RUN_ID_LABEL}={run_id}")


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
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _run_record(reports: Path) -> dict[str, Any]:
    path = reports / "run.json"
    record: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return record


def _describe(items: Iterable[Mapping[str, Any]]) -> str:
    return ", ".join(f"{i['kind']}/{i['metadata']['name']}" for i in items) or "nothing"


def _settled(
    a: Acceptance, run_id: str, *, keep: Callable[[Mapping[str, Any]], bool]
) -> list[dict[str, Any]]:
    """What of *run_id* is still in the cluster besides what *keep* allows, once it settles.

    The CLI removes the Job once it has the reports, and its pods go a moment later.
    """
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        left = [item for item in a.run_owned(run_id) if not keep(item)]
        if not left or time.monotonic() > deadline:
            return left
        time.sleep(3)


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

    left = _settled(a, result.run_id, keep=_is_prometheus)
    a.check(not left, f"only its Prometheus is left in the cluster (also: {_describe(left)})")


def unknown_node(a: Acceptance) -> None:
    """A node the cluster doesn't have: a config error, exit 3, and nothing deployed.

    The nodes are resolved in the Job, in phase 3, so the run is submitted. It stops before
    phase 4 deploys anything, and once the CLI has fetched its log, nothing of it is left.
    """
    result = a.submit("unknown-node", PASSING, target="no-such-node")
    a.check(result.exit_code == 3, f"it exits 3 (got {result.exit_code})")
    a.check(
        "'no-such-node' does not exist" in result.stdout + result.stderr,
        "the run's output names the node that doesn't exist",
    )
    if result.run_id is None:
        return
    left = _settled(a, result.run_id, keep=lambda item: False)
    a.check(not left, f"nothing of the run is left in the cluster ({_describe(left)})")


def preflight_failure(a: Acceptance) -> None:
    """A cluster that fails its preflight: one "cluster not ready" error, exit 4, no results.

    A NetworkPolicy isolates the test's servers while the run is submitted, so the
    preflight's probe from client to server can't connect.
    """
    subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=ISOLATE_SERVERS,
        text=True,
        capture_output=True,
        check=True,
    )
    try:
        result = a.submit("preflight-failure", PASSING)
    finally:
        subprocess.run(
            ["kubectl", "delete", "-f", "-", "--ignore-not-found"],
            input=ISOLATE_SERVERS,
            text=True,
            capture_output=True,
            check=True,
        )
    output = result.stdout + result.stderr
    a.check(result.exit_code == 4, f"it exits 4 (got {result.exit_code})")
    a.check(
        output.count("dokimi: cluster not ready (connectivity)") == 1,
        "one error says the cluster is not ready, and names the connectivity check",
    )
    if result.run_id is None:
        return
    reports = a.reports("preflight-failure", result.run_id)
    for name in ("output.xml", "log.html", "tests.jsonl", "assertions.jsonl"):
        a.check(not (reports / name).exists(), f"no {name}: no test ran")
    record = _run_record(reports)
    a.check(
        "cluster not ready" in str(record.get("stopped")) and record.get("exit_code") is None,
        f"run.json says why it stopped, with no verdict ({record.get('stopped')!r})",
    )
    left = _settled(a, result.run_id, keep=_is_prometheus)
    a.check(not left, f"it keeps nothing but its Prometheus ({_describe(left)})")


def failing_criterion(a: Acceptance) -> None:
    """A criterion the cluster doesn't meet: exit 1, the pair and its value named, and the
    run's evidence kept until ``dokimi cleanup`` removes it."""
    result = a.submit("failing-criterion", FAILING)
    a.check(result.exit_code == 1, f"it exits 1 (got {result.exit_code})")
    pair = f"{a.source} -> {a.target} (pair-001)"
    a.check(
        re.search(re.escape(pair) + r": [0-9.]+ gigabits per second", result.stdout) is not None,
        f"the failure names the pair and its value ({pair}: … gigabits per second)",
    )
    if result.run_id is None:
        return
    reports = a.reports("failing-criterion", result.run_id)
    assertions = _jsonl(reports / "assertions.jsonl")
    a.check(
        [(r["outcome"], r["measured"] is not None) for r in assertions] == [("fail", True)],
        "assertions.jsonl holds the criterion, failed, with the value measured",
    )
    a.check(_run_record(reports).get("retained") == "fail", "run.json says what it kept, and why")
    _kept_until_cleanup(a, result.run_id)


def never_measured(a: Acceptance) -> None:
    """A criterion with no data behind it: metrics_missing, exit 2, never a pass."""
    result = a.submit("never-measured", NEVER_MEASURED)
    a.check(result.exit_code == 2, f"it exits 2 (got {result.exit_code})")
    a.check("metrics_missing" in result.stdout, "the run's output says metrics_missing")
    if result.run_id is None:
        return
    reports = a.reports("never-measured", result.run_id)
    found = [(t["outcome"], t["error_category"]) for t in _jsonl(reports / "tests.jsonl")]
    a.check(
        found == [("error", "metrics_missing")],
        f"tests.jsonl says error, metrics_missing ({found})",
    )
    a.check(
        all(r["outcome"] == "error" for r in _jsonl(reports / "assertions.jsonl")),
        "no assertion passed",
    )
    _kept_until_cleanup(a, result.run_id)


def reattach(a: Acceptance) -> None:
    """The CLI stops following mid-run, the run carries on, and attach shows its result.

    Ctrl-C reaches the CLI while the test is running. The run isn't affected, and
    ``attach``, a new process with only the run id, follows it to its end and fetches the
    same reports a following ``submit`` would have.
    """
    path = a.write("reattach", PASSING)
    follow = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "dokimi.cli.main",
            "submit",
            str(path),
            "--service-account",
            SERVICE_ACCOUNT,
        ],
        cwd=path.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        # A script's background process has SIGINT ignored; Ctrl-C is what's under test.
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
    )
    seen = []
    assert follow.stdout is not None
    for line in follow.stdout:
        seen.append(line)
        if line.startswith("phase 6 (execution)"):
            follow.send_signal(signal.SIGINT)
            break
    rest, _ = follow.communicate(timeout=60)
    output = "".join(seen) + rest
    match = _SUBMITTED.search(output)
    run_id = match["run_id"] if match else None
    if run_id is not None:
        a.runs.append(run_id)
    a.check(
        run_id is not None and follow.returncode == 130,
        f"Ctrl-C mid-run exits 130 (got {follow.returncode})",
    )
    a.check(f"dokimi attach {run_id}" in output, "and says how to reattach")
    if run_id is None:
        return
    directory = a.work / "reattach" / "attached"
    attached = a.dokimi("attach", run_id, "--results-directory", str(directory))
    a.check(
        attached.exit_code == 0,
        f"attach follows the run to its end and exits 0 (got {attached.exit_code})",
    )
    reports = directory / run_id
    missing = [name for name in ARTIFACTS if not (reports / name).is_file()]
    a.check(not missing, f"attach fetched every report (missing: {', '.join(missing) or 'none'})")
    a.check(_run_record(reports).get("outcome") == "pass", "the run passed, unaffected")


def concurrent(a: Acceptance) -> None:
    """Two submissions at the same moment: one runs, the other is refused and leaves nothing.

    Both Jobs start; the first to claim the cluster runs its test, and the other stops in
    its preflight with `run_already_active`, naming the run that holds the cluster.
    """
    processes = []
    for name in ("concurrent-a", "concurrent-b"):
        path = a.write(name, PASSING)
        processes.append(
            a.started("submit", str(path), "--service-account", SERVICE_ACCOUNT, cwd=path.parent)
        )
    results = [a.finished(process) for process in processes]
    a.check(
        sorted(r.exit_code for r in results) == [0, 4],
        f"one exits 0 and the other 4 (got {[r.exit_code for r in results]})",
    )
    winner = next((r for r in results if r.exit_code == 0), None)
    loser = next((r for r in results if r.exit_code == 4), None)
    if winner is None or loser is None or winner.run_id is None or loser.run_id is None:
        return
    refusal = loser.stdout + loser.stderr
    a.check("run_already_active" in refusal, "the refused one says run_already_active")
    a.check(winner.run_id in refusal, "and names the run that holds the cluster")
    left = _settled(a, loser.run_id, keep=lambda item: False)
    a.check(not left, f"the refused one leaves nothing ({_describe(left)})")
    left = _settled(a, winner.run_id, keep=_is_prometheus)
    a.check(not left, f"the one that ran leaves only its Prometheus ({_describe(left)})")


#: What `submit` needs in the run's namespace, word for word from the installation guide's
#: table. A submitter with nothing more, and nothing cluster-wide, must be able to run.
SUBMITTER_RULES = (
    ("", "configmaps", ("create", "get", "delete")),
    ("batch", "jobs", ("create", "list", "delete")),
    ("", "pods", ("get", "list")),
    ("", "pods/log", ("get",)),
)
SUBMITTER = "dokimi-acceptance-submitter"


def submitter_grant() -> str:
    """The submitter's ServiceAccount, its Role of :data:`SUBMITTER_RULES`, and the binding."""
    rules = "".join(
        f"  - apiGroups: [{json.dumps(group)}]\n"
        f"    resources: [{resource}]\n"
        f"    verbs: [{', '.join(verbs)}]\n"
        for group, resource, verbs in SUBMITTER_RULES
    )
    return (
        "apiVersion: v1\nkind: ServiceAccount\n"
        f"metadata: {{name: {SUBMITTER}, namespace: {NAMESPACE}}}\n"
        "---\n"
        f"apiVersion: rbac.authorization.k8s.io/v1\nkind: Role\n"
        f"metadata: {{name: {SUBMITTER}, namespace: {NAMESPACE}}}\nrules:\n{rules}"
        "---\n"
        f"apiVersion: rbac.authorization.k8s.io/v1\nkind: RoleBinding\n"
        f"metadata: {{name: {SUBMITTER}, namespace: {NAMESPACE}}}\n"
        f"roleRef: {{apiGroup: rbac.authorization.k8s.io, kind: Role, name: {SUBMITTER}}}\n"
        f"subjects: [{{kind: ServiceAccount, name: {SUBMITTER}, namespace: {NAMESPACE}}}]\n"
    )


def namespaced_submitter(a: Acceptance) -> None:
    """A submitter with only the namespaced permissions the installation guide lists.

    A ServiceAccount of its own, with a Role holding exactly the guide's `submit` row in the
    run's namespace, submits a passing run through a kubeconfig built from its token.
    """
    grant = submitter_grant()
    kubectl = ["kubectl", "-n", NAMESPACE]
    subprocess.run(
        [*kubectl, "apply", "-f", "-"], input=grant, text=True, capture_output=True, check=True
    )
    try:
        token = subprocess.run(
            [*kubectl, "create", "token", SUBMITTER, "--duration", "1h"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        config = json.loads(
            subprocess.run(
                ["kubectl", "config", "view", "--minify", "--raw", "-o", "json"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
        config["users"] = [{"name": SUBMITTER, "user": {"token": token}}]
        for context in config["contexts"]:
            context["context"]["user"] = SUBMITTER
        kubeconfig = a.work / "submitter.kubeconfig"
        kubeconfig.write_text(json.dumps(config))
        kubeconfig.chmod(0o600)

        path = a.write("namespaced-submitter", PASSING)
        result = a.dokimi(
            "submit",
            str(path),
            "--service-account",
            SERVICE_ACCOUNT,
            "--kubeconfig",
            str(kubeconfig),
            cwd=path.parent,
        )
    finally:
        subprocess.run(
            [*kubectl, "delete", "-f", "-", "--ignore-not-found"],
            input=grant,
            text=True,
            capture_output=True,
            check=True,
        )
    output = result.stdout + result.stderr
    if not a.check(result.exit_code == 0, f"it exits 0 (got {result.exit_code})"):
        print(result.stderr[-3000:])
    a.check("forbidden" not in output.lower(), "nothing it did was forbidden")
    if result.run_id is None:
        return
    left = _settled(a, result.run_id, keep=_is_prometheus)
    a.check(not left, f"it could remove its run: only the Prometheus is left ({_describe(left)})")


def _is_prometheus(item: Mapping[str, Any]) -> bool:
    return _labels(item).get(COMPONENT_LABEL) == PROMETHEUS_COMPONENT


def _kept_until_cleanup(a: Acceptance, run_id: str) -> None:
    """A run that didn't pass keeps its owner, definition and test pods; cleanup removes them."""
    kept = {(item["kind"], _labels(item).get(COMPONENT_LABEL, "")) for item in a.run_owned(run_id)}
    a.check(
        {("Pod", "client"), ("Pod", "server")} <= kept
        and sum(1 for kind, _ in kept if kind == "ConfigMap") >= 1,
        f"it keeps its test pods, owner and definition ({sorted(kept)})",
    )
    removed = a.dokimi("cleanup", run_id)
    a.check(removed.exit_code == 0, f"dokimi cleanup {run_id} exits 0 (got {removed.exit_code})")
    left = _settled(a, run_id, keep=_is_prometheus)
    a.check(not left, f"after which only its Prometheus is left ({_describe(left)})")


SCENARIOS: dict[str, Callable[[Acceptance], None]] = {
    "happy-path": happy_path,
    "unknown-node": unknown_node,
    "preflight-failure": preflight_failure,
    "failing-criterion": failing_criterion,
    "never-measured": never_measured,
    "reattach": reattach,
    "concurrent": concurrent,
    "namespaced-submitter": namespaced_submitter,
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
