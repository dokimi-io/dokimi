"""The iperf2 exporter: one pod's own view, every label, in base units (M1-10).

The exporter is a standalone script in the iperf2 image, standard library only, so it is
loaded from its file here rather than imported from the package. The sample lines are
iperf 2.2.1's ``-y C`` output from a run on kind (dokimi#35).
"""

from __future__ import annotations

import importlib.util
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from dokimi.testtypes.interface import EXPORTER_PORT_NAME, Placement, Role
from dokimi.testtypes.iperf2 import _SCHEMA as SCHEMA
from dokimi.testtypes.iperf2 import CONTAINER_NAME, MEASUREMENT_SOURCE_PORT, Iperf2

SCRIPT = Path(__file__).resolve().parent.parent / "images" / "iperf2" / "dokimi_iperf2_exporter.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dokimi_iperf2_exporter", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


exporter = _load()

CLIENT_IP = "10.244.3.4"
SERVER_IP = "10.244.1.5"
#: The server's report on kind: the warm-up from 5103, the measurement from 5102, and the
#: sum over both, which is not a result.
WARMUP_LINE = (
    f"20261002194722,{CLIENT_IP},5103,{SERVER_IP},5001,"
    "1,0.0-3.0,18723390,49896457,0.012,0,12737,0.000,0"
)
SERVER_LINE = (
    f"20261002194730,{CLIENT_IP},5102,{SERVER_IP},5001,"
    "2,0.0-5.0,31249260,49983531,0.011,0,21258,0.000,0"
)
SUM_LINE = (
    f"20261002194730,{CLIENT_IP},0,0.0.0.0,5001,-1,0.0-8.0,49972650,49869283,nan,0,33995,0.000,0"
)
#: The cold-path warm-up from the same kind run: 356 of 85,010 datagrams lost.
LOSSY_LINE = (
    f"20261002194730,{CLIENT_IP},5102,{SERVER_IP},5001,"
    "2,0.0-10.0,124976700,99981360,0.348,356,85010,0.419,3"
)
#: A UDP client's own summary: it cannot know what arrived.
CLIENT_LINE = (
    f"20261002194730,{CLIENT_IP},5102,{SERVER_IP},5001,"
    "1,0.0-5.0,31250730,50001168,nan,-1,-1,-0.000,0"
)
#: A TCP report has no datagram fields at all.
TCP_LINE = f"20261002194730,{CLIENT_IP},5102,{SERVER_IP},5001,1,0.0-5.0,587202560,939524096"

ENV = {
    "DOKIMI_CLUSTER": "test-cluster",
    "DOKIMI_RUN_ID": "20261002-194700-pd9yru",
    "DOKIMI_TEST_ID": "pair-001",
    "DOKIMI_REPETITION": "1",
    "DOKIMI_TEST_TYPE": "iperf2",
    "DOKIMI_SOURCE_NODE": "worker-01",
    "DOKIMI_TARGET_NODE": "worker-02",
    "DOKIMI_SOURCE_POD": "run-20261002-194700-pd9yru-pair-001-client",
    "DOKIMI_TARGET_POD": "run-20261002-194700-pd9yru-pair-001-server",
    "DOKIMI_PROTOCOL": "udp",
}
#: The required label set, plus ``direction``.
REQUIRED = {
    "cluster",
    "run_id",
    "test_id",
    "repetition",
    "test_type",
    "source_node",
    "target_node",
    "source_pod",
    "target_pod",
    "source_ip",
    "target_ip",
    "protocol",
    "direction",
}
SERIES = re.compile(r"^(?P<name>[a-z0-9_]+)\{(?P<labels>[^}]*)\} (?P<value>\S+)$")
LABEL = re.compile(r'(?P<key>[a-z_]+)="(?P<value>(?:[^"\\]|\\.)*)"')


def labels(role: str) -> Any:
    ip = CLIENT_IP if role == "client" else SERVER_IP
    return exporter.labels_from_environment({**ENV, "DOKIMI_POD_IP": ip})


def results(tmp_path: Path, role: str, csv: str | None = None, **files: str) -> Path:
    (tmp_path / "iperf2.meta").write_text(
        f"role={role}\nwarmup_seconds=10\nmeasurement_source_port={MEASUREMENT_SOURCE_PORT}\n"
    )
    if csv is not None:
        # iperf2 ends every report with a newline; a test of a line cut short writes its own.
        (tmp_path / "iperf2.csv").write_text(csv if csv.endswith("\n") else csv + "\n")
    for name, content in files.items():
        (tmp_path / f"iperf2.{name}").write_text(content)
    return tmp_path


def scrape(directory: Path, role: str) -> dict[str, tuple[dict[str, str], float]]:
    """Each series by name: its labels and its value. Every name appears at most once."""
    found: dict[str, tuple[dict[str, str], float]] = {}
    for line in exporter.render(exporter.read_snapshot(directory), labels(role)).splitlines():
        if line.startswith("#"):
            continue
        match = SERIES.match(line)
        assert match, line
        assert match["name"] not in found
        pairs = {m["key"]: m["value"] for m in LABEL.finditer(match["labels"])}
        found[match["name"]] = (pairs, float(match["value"]))
    return found


def value(series: dict[str, tuple[dict[str, str], float]], name: str) -> float:
    return series[name][1]


# -- the server's view ---------------------------------------------------------------------


def test_the_server_reports_the_measured_connection_and_nothing_else(tmp_path: Path) -> None:
    directory = results(tmp_path, "server", "\n".join([WARMUP_LINE, SERVER_LINE, SUM_LINE]))

    series = scrape(directory, "server")

    assert value(series, "dokimi_test_completed") == 1
    assert value(series, "dokimi_execution_errors_total") == 0
    # The measurement's line, not the warm-up's and not the sum.
    assert value(series, "dokimi_throughput_bits_per_second") == 49983531
    assert value(series, "dokimi_transferred_bytes") == 31249260
    assert value(series, "dokimi_iperf2_datagrams") == 21258


def test_units_are_converted_to_base_units_on_write(tmp_path: Path) -> None:
    # Seconds, not milliseconds; percent is the named exception.
    series = scrape(results(tmp_path, "server", LOSSY_LINE), "server")

    assert value(series, "dokimi_jitter_seconds") == pytest.approx(0.000348)
    assert value(series, "dokimi_throughput_bits_per_second") == 99981360
    # From the counts, more precise than the tool's rounded 0.419.
    assert value(series, "dokimi_packet_loss_percent") == pytest.approx(100 * 356 / 85010)
    assert value(series, "dokimi_iperf2_lost_datagrams") == 356
    assert value(series, "dokimi_iperf2_out_of_order_datagrams") == 3
    assert value(series, "dokimi_test_duration_seconds") == 10


def test_every_series_carries_the_full_required_label_set(tmp_path: Path) -> None:
    cases: list[tuple[str, str | None, dict[str, str]]] = [
        ("server", "\n".join([WARMUP_LINE, SERVER_LINE]), {}),
        ("server", WARMUP_LINE, {}),
        ("client", CLIENT_LINE, {"peer": SERVER_IP, "status": "0"}),
        ("client", None, {"peer": SERVER_IP}),
    ]
    for index, (role, csv, extra) in enumerate(cases):
        directory = tmp_path / str(index)
        directory.mkdir()
        series = scrape(results(directory, role, csv, **extra), role)

        assert series
        for name, (found, _) in series.items():
            assert set(found) == REQUIRED, name
            assert all(found.values()), name


def test_the_pair_is_labelled_the_same_way_from_both_ends(tmp_path: Path) -> None:
    server_dir, client_dir = tmp_path / "server", tmp_path / "client"
    server_dir.mkdir()
    client_dir.mkdir()
    server = scrape(results(server_dir, "server", SERVER_LINE), "server")
    client = scrape(
        results(client_dir, "client", CLIENT_LINE, peer=SERVER_IP, status="0"), "client"
    )

    (server_labels, _) = server["dokimi_test_completed"]
    (client_labels, _) = client["dokimi_test_completed"]
    assert server_labels["direction"] == "inbound"
    assert client_labels["direction"] == "outbound"
    assert {k: v for k, v in server_labels.items() if k != "direction"} == {
        k: v for k, v in client_labels.items() if k != "direction"
    }
    assert server_labels["source_ip"] == CLIENT_IP
    assert server_labels["target_ip"] == SERVER_IP


def test_a_server_that_only_saw_the_warm_up_has_no_result(tmp_path: Path) -> None:
    # For UDP, the measured connection's line is the only proof anything arrived.
    series = scrape(results(tmp_path, "server", WARMUP_LINE), "server")

    assert value(series, "dokimi_test_completed") == 0
    assert "dokimi_throughput_bits_per_second" not in series


def test_a_server_that_saw_nothing_serves_no_series(tmp_path: Path) -> None:
    # It cannot name the client yet; a series without source_ip could match nothing.
    assert scrape(results(tmp_path, "server", ""), "server") == {}


def test_nothing_is_served_before_the_role_script_wrote_its_meta(tmp_path: Path) -> None:
    assert exporter.read_snapshot(tmp_path) is None
    assert exporter.render(None, labels("server")) == ""


# -- the client's view ---------------------------------------------------------------------


def test_a_client_not_yet_started_serves_no_series(tmp_path: Path) -> None:
    assert scrape(results(tmp_path, "client"), "client") == {}


def test_a_started_client_is_not_yet_complete(tmp_path: Path) -> None:
    series = scrape(results(tmp_path, "client", peer=SERVER_IP), "client")

    assert value(series, "dokimi_test_completed") == 0
    assert value(series, "dokimi_execution_errors_total") == 0
    assert series["dokimi_test_completed"][0]["target_ip"] == SERVER_IP


def test_a_finished_udp_client_reports_what_it_sent_and_no_loss(tmp_path: Path) -> None:
    # Loss and jitter are the receiver's to report; the client's own line has none.
    series = scrape(results(tmp_path, "client", CLIENT_LINE, peer=SERVER_IP, status="0"), "client")

    assert value(series, "dokimi_test_completed") == 1
    assert value(series, "dokimi_throughput_bits_per_second") == 50001168
    assert "dokimi_packet_loss_percent" not in series
    assert "dokimi_jitter_seconds" not in series
    assert "dokimi_iperf2_datagrams" not in series


def test_a_tcp_client_reports_its_throughput(tmp_path: Path) -> None:
    series = scrape(results(tmp_path, "client", TCP_LINE, peer=SERVER_IP, status="0"), "client")

    assert value(series, "dokimi_throughput_bits_per_second") == 939524096


@pytest.mark.parametrize(
    ("csv", "status"),
    [
        (CLIENT_LINE, "1"),  # iperf2 failed
        ("", "0"),  # exited 0 without a summary line: measured nothing (the script's 69)
    ],
)
def test_a_client_that_ended_without_a_measurement_is_an_execution_error(
    tmp_path: Path, csv: str, status: str
) -> None:
    series = scrape(results(tmp_path, "client", csv, peer=SERVER_IP, status=status), "client")

    assert value(series, "dokimi_test_completed") == 0
    assert value(series, "dokimi_execution_errors_total") == 1
    assert "dokimi_throughput_bits_per_second" not in series


def test_lines_that_are_not_reports_are_counted_not_guessed_at(tmp_path: Path) -> None:
    csv = "\n".join(["iperf: connect failed", SERVER_LINE, "1,2,3"])

    series = scrape(results(tmp_path, "server", csv), "server")

    assert value(series, "dokimi_iperf2_unparsed_lines") == 2
    assert value(series, "dokimi_test_completed") == 1


def test_a_line_still_being_written_is_not_a_result(tmp_path: Path) -> None:
    # The server writes while it runs; M1-12 reads right as the connection ends. A TCP line
    # cut inside its last field still has nine fields.
    directory = results(tmp_path, "server")
    (directory / "iperf2.csv").write_text(WARMUP_LINE + "\n" + TCP_LINE[:-6])

    series = scrape(directory, "server")

    assert value(series, "dokimi_test_completed") == 0
    assert "dokimi_throughput_bits_per_second" not in series
    assert value(series, "dokimi_iperf2_unparsed_lines") == 0


def test_a_line_counts_once_its_newline_arrives(tmp_path: Path) -> None:
    directory = results(tmp_path, "server")
    (directory / "iperf2.csv").write_text(WARMUP_LINE + "\r\n" + SERVER_LINE + "\r\n")

    series = scrape(directory, "server")

    assert value(series, "dokimi_test_completed") == 1
    assert value(series, "dokimi_throughput_bits_per_second") == 49983531


# -- labels ----------------------------------------------------------------------------------


def test_it_refuses_to_start_without_a_required_label(capsys: pytest.CaptureFixture[str]) -> None:
    environment = {k: v for k, v in ENV.items() if k != "DOKIMI_CLUSTER"}

    assert exporter.main(["--results", "/results", "--port", "9101"], env=environment) == 64
    err = capsys.readouterr().err
    assert "DOKIMI_CLUSTER" in err
    assert "DOKIMI_POD_IP" in err


def test_label_values_are_escaped() -> None:
    lines: list[str] = []
    exporter._metric(lines, "dokimi_x", "gauge", "x", {"a": 'q"b\\c\nd'}, 1)

    assert lines[-1] == 'dokimi_x{a="q\\"b\\\\c\\nd"} 1\n'


# -- serving ---------------------------------------------------------------------------------


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[Path, str]]:
    server = exporter.serve(tmp_path, 0, labels("client"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield tmp_path, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_metrics_still_serves_the_completed_result_after_the_tool_exited(
    served: tuple[Path, str],
) -> None:
    # Acceptance criterion: the exporter reads the files on every request, and iperf2
    # exiting leaves them in place.
    directory, url = served
    results(directory, "client", CLIENT_LINE, peer=SERVER_IP, status="0")

    for _ in range(2):
        with urllib.request.urlopen(f"{url}/metrics", timeout=5) as response:
            body = response.read().decode()
            assert response.headers["Content-Type"].startswith("text/plain; version=0.0.4")
        assert re.search(r"^dokimi_test_completed\{.*\} 1$", body, re.M)
        assert re.search(r"^dokimi_throughput_bits_per_second\{.*\} 50001168$", body, re.M)


def test_only_metrics_is_served(served: tuple[Path, str]) -> None:
    _, url = served

    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(f"{url}/", timeout=5)
    assert refused.value.code == 404


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is a POSIX signal")
def test_it_exits_at_once_on_sigterm(tmp_path: Path) -> None:
    # As a pod's PID 1 without a handler it would ignore SIGTERM, and every test pod would
    # wait out its grace period.
    process = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--results", str(tmp_path), "--port", "0"],
        env={**os.environ, **ENV, "DOKIMI_POD_IP": CLIENT_IP},
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stderr is not None
        assert "serving /metrics" in process.stderr.readline()
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5) == 0
        assert time.monotonic() - started < 2
    finally:
        process.kill()
        process.wait()


# -- the pod spec ----------------------------------------------------------------------------

PLACEMENT = Placement(
    "dokimi-iperf2", ENV["DOKIMI_RUN_ID"], "pair-001", "worker-01", "worker-02", "test-cluster"
)


def pods() -> dict[Role, Any]:
    params = SCHEMA.validate({"protocol": "udp", "targetBandwidth": "100M", "duration": "5s"})
    image = "ghcr.io/dokimi-io/iperf2@sha256:" + "b" * 64
    return dict(Iperf2(image=image).workload(params, PLACEMENT).pods)


def sidecar(pod: Any) -> dict[str, Any]:
    (found,) = [each for each in pod["spec"]["containers"] if each["name"] == "exporter"]
    result: dict[str, Any] = found
    return result


def test_every_pod_runs_the_exporter_next_to_the_tool() -> None:
    for pod in pods().values():
        names = [each["name"] for each in pod["spec"]["containers"]]
        assert names == [CONTAINER_NAME, "exporter"]
        # A regular container: a native sidecar stops when the tool does.
        assert "initContainers" not in pod["spec"]
        (tool,) = [each for each in pod["spec"]["containers"] if each["name"] == CONTAINER_NAME]
        assert sidecar(pod)["image"] == tool["image"]


def test_the_exporter_is_given_every_fixed_label_with_the_pairs_values() -> None:
    for role, pod in pods().items():
        env = {entry["name"]: entry.get("value") for entry in sidecar(pod)["env"]}

        assert set(exporter.LABEL_ENVIRONMENT.values()) | {"DOKIMI_POD_IP"} == set(env)
        assert env == {
            **ENV,
            "DOKIMI_RUN_ID": PLACEMENT.run_id,
            "DOKIMI_SOURCE_POD": pods()[Role.CLIENT]["metadata"]["name"],
            "DOKIMI_TARGET_POD": pods()[Role.SERVER]["metadata"]["name"],
            "DOKIMI_POD_IP": None,
        }, role
        (pod_ip,) = [entry for entry in sidecar(pod)["env"] if entry["name"] == "DOKIMI_POD_IP"]
        assert pod_ip["valueFrom"] == {"fieldRef": {"fieldPath": "status.podIP"}}


def test_the_exporter_serves_where_prometheus_and_the_verdict_look() -> None:
    spec = Iperf2(image="iperf2:dev").exporter()
    for pod in pods().values():
        container = sidecar(pod)
        (port,) = container["ports"]

        # The managed Prometheus scrapes exactly the ports named this way.
        assert port == {"name": EXPORTER_PORT_NAME, "containerPort": spec.port, "protocol": "TCP"}
        assert container["args"] == ["--results", spec.results_directory, "--port", str(spec.port)]
        assert container["readinessProbe"]["httpGet"]["path"] == spec.path


def test_the_exporter_only_reads_the_results() -> None:
    for pod in pods().values():
        container = sidecar(pod)

        assert container["volumeMounts"] == [
            {"name": "results", "mountPath": "/results", "readOnly": True}
        ]
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        assert container["resources"]["limits"]["memory"]


def test_the_command_is_the_one_the_image_installs() -> None:
    dockerfile = (SCRIPT.parent / "Dockerfile").read_text()
    (command,) = sidecar(pods()[Role.CLIENT])["command"]

    assert f"images/iperf2/{SCRIPT.name} /usr/local/bin/{command}" in dockerfile
