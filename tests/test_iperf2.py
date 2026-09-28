"""The iperf2 plugin: its capabilities, its parameters, its pods, and how it starts and ends.

Two of M1-09's acceptance criteria live here — capabilities differ between TCP and UDP,
and the warm-up pass is visible in run metadata and can be disabled. The third, that
``start()`` is never called before both pods are ``Ready``, is in ``test_lifecycle.py``,
with ``start()``'s own refusal here as its second line of defence.
"""

from __future__ import annotations

import pathlib
import re
import socket
import threading
from typing import Any

import pytest

import dokimi._image_digest as image_digest
from dokimi.k8s.resources import POD, Manifest
from dokimi.model import ConfigError, Duration
from dokimi.testtypes.images import WorkloadImageError
from dokimi.testtypes.interface import (
    Capability,
    ClusterFacts,
    DeployedWorkload,
    ImageSource,
    Placement,
    Role,
    TestParameters,
)
from dokimi.testtypes.iperf2 import (
    CONTAINER_NAME,
    IPERF_PORT,
    TRIGGER_PORT,
    Completion,
    Iperf2,
    tcp_trigger,
)
from dokimi.testtypes.lifecycle import NotReadyError, WorkloadLostError
from tests.fake_resources import FakeResources

DIGEST = "sha256:" + "b" * 64
IMAGE = f"ghcr.io/dokimi-io/iperf2@{DIGEST}"
PLACEMENT = Placement(
    namespace="dokimi-iperf2",
    run_id="20260924-143012-k3f9x2",
    test_id="pair-001",
    source_node="worker-01",
    target_node="worker-02",
)
CLIENT_SCRIPT = (
    pathlib.Path(__file__).resolve().parent.parent / "images" / "iperf2" / "dokimi-iperf2-client.sh"
)
DNS_1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def params(**overrides: str) -> TestParameters:
    raw = {"protocol": "udp", "targetBandwidth": "1G", "duration": "30s"} | overrides
    return Iperf2().parameter_schema().validate(raw)


def container(pod: Manifest) -> dict[str, Any]:
    (only,) = pod["spec"]["containers"]
    assert only["name"] == CONTAINER_NAME
    result: dict[str, Any] = only
    return result


def client_args(**overrides: str) -> dict[str, str]:
    args = container(
        Iperf2(image=IMAGE).workload(params(**overrides), PLACEMENT).pods[Role.CLIENT]
    )["args"]
    return dict(zip(args[::2], args[1::2], strict=True))


# -- capabilities ------------------------------------------------------------------------


def test_capabilities_answer_differently_for_tcp_and_udp() -> None:
    # The acceptance criterion, and ADR-02-01's reason for keying on the parameter set:
    # jitter and packet loss exist only over UDP.
    plugin = Iperf2()

    assert plugin.capabilities(params(protocol="tcp")) == {Capability.THROUGHPUT}
    assert plugin.capabilities(params(protocol="udp")) == {
        Capability.THROUGHPUT,
        Capability.PACKET_LOSS,
        Capability.JITTER,
    }


def test_capabilities_ignore_the_environment() -> None:
    # iperf2 needs no fact about the cluster, which is why the argument costs it nothing.
    plugin = Iperf2()

    assert plugin.capabilities(params(), ClusterFacts()) == plugin.capabilities(params())


# -- parameters --------------------------------------------------------------------------


def test_the_schema_is_the_paper_design_plus_warm_up() -> None:
    assert Iperf2().parameter_schema().names == (
        "protocol",
        "targetBandwidth",
        "duration",
        "warmup",
    )


def test_warm_up_runs_by_default() -> None:
    # ADR-04-04: on unless disabled.
    assert params().get("warmup", Duration).seconds > 0


def test_warm_up_can_be_disabled() -> None:
    assert params(warmup="0s").get("warmup", Duration).seconds == 0
    assert params(warmup="0").get("warmup", Duration).seconds == 0


def test_a_malformed_warm_up_says_how_to_disable_it() -> None:
    with pytest.raises(ConfigError, match="'0s' to disable"):
        params(warmup="soon")


def test_an_unknown_protocol_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="Accepted values are: 'udp', 'tcp'"):
        params(protocol="sctp")


def test_a_malformed_bandwidth_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="malformed bandwidth"):
        params(targetBandwidth="fast")


# -- images ------------------------------------------------------------------------------


def test_both_roles_run_the_same_image() -> None:
    images = Iperf2(image=IMAGE).images()

    assert images.client == images.server
    assert images.client.reference == IMAGE


def test_the_release_default_is_the_baked_in_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_digest, "IPERF2_IMAGE_DIGEST", DIGEST)

    image = Iperf2().images().client

    assert image.reference == IMAGE
    assert image.source is ImageSource.RELEASE_DEFAULT


def test_a_checkout_needs_an_explicit_image() -> None:
    with pytest.raises(WorkloadImageError):
        Iperf2().images()


# -- the workload ------------------------------------------------------------------------


def test_the_warm_up_is_in_the_run_metadata() -> None:
    # The acceptance criterion: never silent, and 0 rather than absent when disabled.
    plugin = Iperf2(image=IMAGE)

    enabled = plugin.workload(params(warmup="5s"), PLACEMENT).run_metadata
    disabled = plugin.workload(params(warmup="0s"), PLACEMENT).run_metadata

    assert enabled["warmup_duration_seconds"] == "5"
    assert disabled["warmup_duration_seconds"] == "0"


def test_the_warm_up_the_metadata_records_is_the_one_the_client_runs() -> None:
    metadata = Iperf2(image=IMAGE).workload(params(warmup="5s"), PLACEMENT).run_metadata

    assert client_args(warmup="5s")["--warmup"] == metadata["warmup_duration_seconds"]
    assert client_args(warmup="0s")["--warmup"] == "0"


def test_the_run_metadata_attributes_the_result() -> None:
    # ADR-05-04: the digest identifies what was measured. ADR-05-03: so does the mode.
    metadata = Iperf2(image=IMAGE).workload(params(), PLACEMENT).run_metadata

    assert metadata["workload_image"] == IMAGE
    assert metadata["workload_image_digest"] == DIGEST
    assert metadata["workload_image_source"] == "override"
    assert metadata["connectivity_mode"] == "podNetwork"
    assert metadata["protocol"] == "udp"


def test_the_client_sends_from_the_source_and_the_server_receives_on_the_target() -> None:
    pods = Iperf2(image=IMAGE).workload(params(), PLACEMENT).pods

    def node(pod: Manifest) -> str:
        affinity = pod["spec"]["affinity"]["nodeAffinity"]
        (term,) = affinity["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
        (field,) = term["matchFields"]
        assert field["key"] == "metadata.name"
        (value,) = field["values"]
        return str(value)

    assert node(pods[Role.CLIENT]) == "worker-01"
    assert node(pods[Role.SERVER]) == "worker-02"


def test_pods_go_through_the_scheduler() -> None:
    # nodeName would bypass it, and the readiness gate could never report the scheduling
    # error ADR-05-07 asks for.
    for pod in Iperf2(image=IMAGE).workload(params(), PLACEMENT).pods.values():
        assert "nodeName" not in pod["spec"]


def test_the_server_is_created_before_the_client() -> None:
    assert list(Iperf2(image=IMAGE).workload(params(), PLACEMENT).pods) == [
        Role.SERVER,
        Role.CLIENT,
    ]


def test_pods_are_minimal_and_non_root() -> None:
    for pod in Iperf2(image=IMAGE).workload(params(), PLACEMENT).pods.values():
        spec = pod["spec"]
        security = container(pod)["securityContext"]

        assert spec["securityContext"]["runAsNonRoot"] is True
        assert spec["automountServiceAccountToken"] is False
        assert spec["restartPolicy"] == "Never"
        assert security["readOnlyRootFilesystem"] is True
        assert security["allowPrivilegeEscalation"] is False
        assert security["capabilities"] == {"drop": ["ALL"]}
        assert "hostNetwork" not in spec  # ADR-05-03: pod network only


def test_pod_names_are_valid_object_names() -> None:
    for pod in Iperf2(image=IMAGE).workload(params(), PLACEMENT).pods.values():
        name = pod["metadata"]["name"]
        assert DNS_1123_LABEL.match(name) and len(name) <= 63, name


def test_the_client_measures_in_base_units() -> None:
    args = client_args(targetBandwidth="1.5G", duration="1500ms")

    assert args["--bandwidth"] == "1500000000"
    assert args["--duration"] == "1.5"
    assert args["--protocol"] == "udp"


@pytest.mark.parametrize(("protocol", "flag"), [("udp", True), ("tcp", False)])
def test_the_server_listens_on_the_measured_protocol(protocol: str, flag: bool) -> None:
    server = container(
        Iperf2(image=IMAGE).workload(params(protocol=protocol), PLACEMENT).pods[Role.SERVER]
    )

    assert ("-u" in server["command"]) is flag
    assert server["ports"] == [
        {"name": "iperf", "containerPort": IPERF_PORT, "protocol": protocol.upper()}
    ]


def test_no_readiness_probe_connects_to_the_trigger_port() -> None:
    # A tcpSocket probe on the client's trigger port would be a trigger itself, and would
    # start the measurement before the gate has passed.
    for pod in Iperf2(image=IMAGE).workload(params(), PLACEMENT).pods.values():
        probe = container(pod)["readinessProbe"]
        assert "tcpSocket" not in probe
        assert "exec" in probe


def test_each_probe_checks_the_port_its_container_serves() -> None:
    for pod in Iperf2(image=IMAGE).workload(params(), PLACEMENT).pods.values():
        spec = container(pod)
        (port,) = spec["ports"]
        assert f":{port['containerPort']} " in " ".join(spec["readinessProbe"]["exec"]["command"])


def test_the_client_script_accepts_exactly_the_flags_the_plugin_passes() -> None:
    # The script is the contract between the orchestrator and the pod, and it lives in
    # the image. Nothing else would notice the two drifting apart until a real run.
    script = CLIENT_SCRIPT.read_text()
    accepted = set(re.findall(r"^\s+(--[a-z-]+)\)", script, re.MULTILINE))

    assert accepted == set(client_args())


# -- start, completion, cleanup ----------------------------------------------------------


def ready(pod: Manifest, ip: str) -> Manifest:
    return pod | {
        "status": {
            "phase": "Running",
            "podIP": ip,
            "conditions": [{"type": "Ready", "status": "True"}],
        }
    }


def deployed(
    cluster: FakeResources | None = None, *, client_ready: bool = True
) -> DeployedWorkload:
    spec = Iperf2(image=IMAGE).workload(params(), PLACEMENT)
    client = ready(spec.pods[Role.CLIENT], "10.0.1.5")
    if not client_ready:
        client = client | {"status": {"phase": "Pending"}}
    return DeployedWorkload(
        spec=spec,
        pods={Role.SERVER: ready(spec.pods[Role.SERVER], "10.0.2.7"), Role.CLIENT: client},
        cluster=cluster if cluster is not None else FakeResources(),
    )


def test_start_sends_the_server_ip_to_the_client_trigger() -> None:
    sent: list[tuple[str, int, bytes]] = []
    plugin = Iperf2(image=IMAGE, trigger=lambda *call: sent.append(call))

    plugin.start(deployed())

    assert sent == [("10.0.1.5", TRIGGER_PORT, b"10.0.2.7\n")]


def test_start_refuses_a_pod_that_is_not_ready() -> None:
    sent: list[object] = []
    plugin = Iperf2(image=IMAGE, trigger=lambda *call: sent.append(call))

    with pytest.raises(NotReadyError, match="client pod"):
        plugin.start(deployed(client_ready=False))
    assert sent == []


def _client_status(cluster: FakeResources, state: dict[str, Any]) -> DeployedWorkload:
    workload = deployed(cluster)
    pod = workload.spec.pods[Role.CLIENT]
    stored = cluster.put(POD, PLACEMENT.namespace, pod)
    stored["status"] = {"containerStatuses": [{"name": CONTAINER_NAME, "state": state}]}
    cluster.objects[("Pod", PLACEMENT.namespace, pod["metadata"]["name"])] = stored
    return workload


def test_a_running_client_is_not_complete() -> None:
    cluster = FakeResources()
    workload = _client_status(cluster, {"running": {"startedAt": "now"}})

    assert Iperf2().is_complete(workload) is False


def test_completion_is_the_client_process_exiting_with_its_summary_line() -> None:
    cluster = FakeResources()
    summary = "20260924143100,10.0.1.5,40000,10.0.2.7,5001,1,0.0-30.0,3750000000,1000000000"
    workload = _client_status(
        cluster, {"terminated": {"exitCode": 0, "reason": "Completed", "message": summary}}
    )
    plugin = Iperf2()

    assert plugin.is_complete(workload) is True
    assert plugin.completion(workload) == Completion(0, summary, "Completed")


def test_a_failed_client_is_complete_and_says_so() -> None:
    # Complete is not passed: the verdict belongs to later phases, which need the status.
    cluster = FakeResources()
    workload = _client_status(cluster, {"terminated": {"exitCode": 1, "reason": "Error"}})

    completion = Iperf2().completion(workload)

    assert completion is not None
    assert not completion.succeeded
    assert completion.summary == ""


def test_a_client_that_disappeared_is_lost_not_incomplete() -> None:
    with pytest.raises(WorkloadLostError):
        Iperf2().is_complete(deployed(FakeResources()))


def test_cleanup_needs_nothing_beyond_pod_deletion() -> None:
    cluster = FakeResources()
    workload = deployed(cluster)

    Iperf2().cleanup(workload)

    assert cluster.created == [] and cluster.deleted == []


# -- the trigger -------------------------------------------------------------------------


def test_the_tcp_trigger_delivers_its_payload() -> None:
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        received: list[bytes] = []

        def accept() -> None:
            connection, _ = server.accept()
            with connection:
                received.append(connection.recv(64))

        listener = threading.Thread(target=accept)
        listener.start()
        tcp_trigger("127.0.0.1", port, b"10.0.2.7\n")
        listener.join(timeout=5)

    assert received == [b"10.0.2.7\n"]


def test_the_tcp_trigger_gives_up_after_its_attempts() -> None:
    with socket.create_server(("127.0.0.1", 0)) as probe:
        port = probe.getsockname()[1]  # closed on exit: nothing listens there any more
    slept: list[float] = []

    with pytest.raises(ConnectionError, match="could not start the client"):
        tcp_trigger("127.0.0.1", port, b"x", attempts=3, timeout=1, sleep=slept.append)
    assert slept == [1.0, 1.0]
