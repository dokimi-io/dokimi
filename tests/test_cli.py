"""The CLI: what each command does, and what it exits with.

Exit codes are automation-facing from the first release (ADR-01-08), so they are asserted
as values rather than as "non-zero".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_orchestrator import images
from test_orchestrator.cli import commands
from test_orchestrator.cli.exit_codes import ExitCode
from test_orchestrator.cli.main import main
from test_orchestrator.images import IMAGE_OVERRIDE_ENV_VAR, IMAGE_REPOSITORY
from tests.fake_cluster import ExplodingCluster, FakeCluster, node

DIGEST = "sha256:" + "ab" * 32
IMAGE = f"{IMAGE_REPOSITORY}@{DIGEST}"
RUN_ID = "20260101-100000-4f2a9c"
EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "worker-01-to-worker-02-udp.yaml"


@pytest.fixture(autouse=True)
def _released_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most commands need an image to submit; a checkout has none by design."""
    monkeypatch.delenv(IMAGE_OVERRIDE_ENV_VAR, raising=False)
    monkeypatch.setattr(images, "ORCHESTRATOR_IMAGE_DIGEST", DIGEST)


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> FakeCluster:
    fake = FakeCluster(log_lines=["run started", "run finished"], exit_code=0)
    monkeypatch.setattr(commands, "connect", lambda **_kwargs: fake)
    return fake


@pytest.fixture
def no_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commands, "connect", lambda **_kwargs: ExplodingCluster())


# -- validate -----------------------------------------------------------------


def test_validate_accepts_the_example(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(EXAMPLE)]) == ExitCode.PASS
    out = capsys.readouterr().out
    assert "is valid" in out
    assert "worker-01 -> worker-02" in out


def test_validate_rejects_a_malformed_definition(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("apiVersion: test-orchestrator/v1alpha1\nkind: NetworkTest\n")

    assert main(["validate", str(broken)]) == ExitCode.CONFIG_ERROR
    assert capsys.readouterr().err.startswith("test-orchestrator: ")


def test_a_missing_file_is_a_config_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", "/nonexistent/test.yaml"]) == ExitCode.CONFIG_ERROR
    assert "no such file" in capsys.readouterr().err


def test_an_unreadable_format_is_a_config_error(tmp_path: Path) -> None:
    unknown = tmp_path / "test.txt"
    unknown.write_text("whatever")

    assert main(["validate", str(unknown)]) == ExitCode.CONFIG_ERROR


# -- dry run ------------------------------------------------------------------


def test_dry_run_renders_the_matrix_and_the_manifests(
    no_cluster: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["submit", str(EXAMPLE), "--dry-run"]) == ExitCode.PASS

    captured = capsys.readouterr()
    # Manifests on stdout, everything a person reads on stderr, so the rendering can be
    # saved or diffed without a summary mixed into it.
    assert "kind: Job" in captured.out
    assert "kind: ConfigMap" in captured.out
    assert "Planned matrix (1 test, sequential" in captured.err
    assert "worker-01 -> worker-02" in captured.err
    assert "nothing was created" in captured.err


def test_dry_run_touches_no_cluster(no_cluster: None) -> None:
    # The fixture's client raises on contact, so this passing is the assertion: not one
    # call was made. ADR-01-06 requires --dry-run to stay fully local.
    assert main(["submit", str(EXAMPLE), "--dry-run"]) == ExitCode.PASS


def test_dry_run_rejects_invalid_input_before_anything_else(
    no_cluster: None, tmp_path: Path
) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("kind: NetworkTest\n")

    assert main(["submit", str(broken), "--dry-run"]) == ExitCode.CONFIG_ERROR


def test_invalid_input_never_reaches_the_cluster(no_cluster: None, tmp_path: Path) -> None:
    # Same client that raises on contact, but this time without --dry-run: validation
    # must fail first (ADR-05-05's config_error, detected before deployment).
    broken = tmp_path / "broken.yaml"
    broken.write_text("kind: NetworkTest\n")

    assert main(["submit", str(broken)]) == ExitCode.CONFIG_ERROR


def test_a_development_build_has_no_image_to_submit(
    no_cluster: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(images, "ORCHESTRATOR_IMAGE_DIGEST", None)

    assert main(["submit", str(EXAMPLE), "--dry-run"]) == ExitCode.CONFIG_ERROR
    assert "development build" in capsys.readouterr().err


# -- submit -------------------------------------------------------------------


def test_submit_creates_the_run_and_streams_it(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["submit", str(EXAMPLE), "--run-id", RUN_ID]) == ExitCode.PASS

    captured = capsys.readouterr()
    assert cluster.created_kinds == ["ConfigMap", "ConfigMap", "Job"]
    assert "run started" in captured.out
    assert f"submitted run {RUN_ID}" in captured.err


def test_submit_propagates_the_runs_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(commands, "connect", lambda **_k: FakeCluster(exit_code=ExitCode.FAIL))

    assert main(["submit", str(EXAMPLE)]) == ExitCode.FAIL


def test_no_follow_returns_immediately_with_a_way_back(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["submit", str(EXAMPLE), "--run-id", RUN_ID, "--no-follow"]) == ExitCode.PASS

    assert cluster.streamed == []
    assert f"attach {RUN_ID}" in capsys.readouterr().err


def test_submit_honours_the_namespace_override(cluster: FakeCluster) -> None:
    main(["submit", str(EXAMPLE), "--namespace", "somewhere-else"])

    assert cluster.namespaces == ["somewhere-else"]


# -- attach -------------------------------------------------------------------


def test_attach_resumes_streaming_a_run_it_never_submitted(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["attach", RUN_ID]) == ExitCode.PASS

    assert cluster.created == [], "attaching must not create anything"
    assert "run finished" in capsys.readouterr().out


def test_attach_needs_only_a_run_id_and_cluster_access(cluster: FakeCluster) -> None:
    # Nothing local is consulted: no definition, no state file, no record of the
    # submitting process. That is what makes reattach work from another machine.
    assert main(["attach", RUN_ID]) == ExitCode.PASS
    assert cluster.streamed


def test_attach_propagates_the_runs_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commands, "connect", lambda **_k: FakeCluster(exit_code=ExitCode.ERROR))

    assert main(["attach", RUN_ID]) == ExitCode.ERROR


def test_attach_rejects_a_malformed_run_id(
    no_cluster: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["attach", "yesterdays-run"]) == ExitCode.CONFIG_ERROR
    assert "malformed run id" in capsys.readouterr().err


def test_attach_reports_a_run_that_is_not_there(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(commands, "connect", lambda **_k: FakeCluster(has_pod=False, has_job=False))

    assert main(["attach", RUN_ID]) == ExitCode.CONFIG_ERROR
    assert "no run" in capsys.readouterr().err


def test_interrupting_leaves_the_run_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupt(*_args: object, **_kwargs: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(commands, "connect", lambda **_k: FakeCluster())
    monkeypatch.setattr(commands, "follow", interrupt)

    assert main(["attach", RUN_ID]) == ExitCode.INTERRUPTED
    err = capsys.readouterr().err
    assert "continues in the cluster" in err
    assert f"attach {RUN_ID}" in err


# -- execute (the in-cluster side) --------------------------------------------


def test_execute_parses_then_plans(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["execute", "--definition", str(EXAMPLE), "--run-id", RUN_ID])

    out = capsys.readouterr()
    assert f"run {RUN_ID}" in out.out
    assert "phase 2 (ingestion): definition parsed" in out.out
    assert "phase 3 (planning): 1 test, sequential" in out.out
    assert "pair-001  worker-01 -> worker-02" in out.out
    assert cluster.node_reads == 1
    # Phases 4 to 11 do not exist yet, and a run that stops early must not look like
    # one that passed.
    assert code == ExitCode.INTERNAL
    assert "phases 4 to 11 are not implemented" in out.err


def test_execute_rejects_a_missing_node_as_a_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCluster(nodes=[node("worker-01"), node("worker-03")])
    monkeypatch.setattr(commands, "connect", lambda **_k: fake)

    code = main(["execute", "--definition", str(EXAMPLE), "--run-id", RUN_ID])

    assert code == ExitCode.CONFIG_ERROR
    assert "target node 'worker-02' does not exist" in capsys.readouterr().err
    # Resolution happens before phase 6 deploys anything, so there is nothing to undo.
    assert fake.created == []


def test_execute_rejects_an_unschedulable_node_as_a_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCluster(nodes=[node("worker-01"), node("worker-02", cordoned=True)])
    monkeypatch.setattr(commands, "connect", lambda **_k: fake)

    code = main(["execute", "--definition", str(EXAMPLE), "--run-id", RUN_ID])

    assert code == ExitCode.CONFIG_ERROR
    assert "cordoned" in capsys.readouterr().err


# -- the plan in a dry run ----------------------------------------------------


def test_a_plain_dry_run_says_the_nodes_were_not_checked(
    no_cluster: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["submit", str(EXAMPLE), "--dry-run"]) == ExitCode.PASS

    err = capsys.readouterr().err
    assert "nodes not checked" in err
    assert "Nodes were not checked against a cluster" in err


def test_a_dry_run_with_a_kubeconfig_shows_the_resolved_plan(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["submit", str(EXAMPLE), "--dry-run", "--kubeconfig", "/some/kubeconfig"])

    err = capsys.readouterr().err
    assert code == ExitCode.PASS
    assert "Planned matrix (1 test, sequential)" in err
    assert "pair-001  worker-01 -> worker-02" in err
    assert "Both nodes exist and can accept a test pod" in err


def test_a_dry_run_with_a_kubeconfig_only_reads(cluster: FakeCluster) -> None:
    # ADR-01-06 allows a provided kubeconfig "for read-only cluster info". Read-only
    # means the fake records not one write.
    main(["submit", str(EXAMPLE), "--dry-run", "--context", "kind-test"])

    assert cluster.node_reads == 1
    assert cluster.created == []
    assert cluster.namespaces == []


def test_a_dry_run_with_a_kubeconfig_catches_a_missing_node(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCluster(nodes=[node("worker-01")])
    monkeypatch.setattr(commands, "connect", lambda **_k: fake)

    code = main(["submit", str(EXAMPLE), "--dry-run", "--context", "kind-test"])

    assert code == ExitCode.CONFIG_ERROR
    assert "target node 'worker-02' does not exist" in capsys.readouterr().err


# -- image and top level ------------------------------------------------------


def test_image_prints_the_released_default(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["image"]) == ExitCode.PASS
    out = capsys.readouterr().out
    assert IMAGE in out
    assert "source: release-default" in out


def test_image_honours_the_run_override(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["image", "--orchestrator-image", "registry.internal/orchestrator:1.2.3"]) == 0
    out = capsys.readouterr().out
    assert "registry.internal/orchestrator:1.2.3" in out
    assert "source: run-override" in out


def test_image_honours_the_installation_override(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(IMAGE_OVERRIDE_ENV_VAR, f"registry.internal/orchestrator@{DIGEST}")

    assert main(["image"]) == ExitCode.PASS
    assert "source: installation-override" in capsys.readouterr().out


def test_image_on_a_development_build_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(images, "ORCHESTRATOR_IMAGE_DIGEST", None)

    assert main(["image"]) == ExitCode.CONFIG_ERROR
    assert "development build" in capsys.readouterr().err


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == ExitCode.PASS
    assert "usage: test-orchestrator" in capsys.readouterr().out


def test_version_reports_the_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith("test-orchestrator ")


def test_an_unknown_command_is_a_config_error() -> None:
    # argparse would exit 2, which ADR-01-08 reserves for `error`: a typo on the command
    # line would read to CI as a run that errored. It is an invalid request: 3.
    with pytest.raises(SystemExit) as exit_info:
        main(["teleport"])

    assert exit_info.value.code == ExitCode.CONFIG_ERROR


def test_an_unknown_flag_is_a_config_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["submit", str(EXAMPLE), "--dry-rn"])

    assert exit_info.value.code == ExitCode.CONFIG_ERROR
    assert "unrecognized arguments: --dry-rn" in capsys.readouterr().err


def test_help_still_exits_cleanly() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["submit", "--help"])

    assert exit_info.value.code == 0


def test_a_bad_run_id_on_submit_is_a_config_error(
    no_cluster: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # Previously only `attach` validated it; `submit` sent it to the API server.
    assert (
        main(["submit", str(EXAMPLE), "--run-id", "My Run", "--dry-run"]) == ExitCode.CONFIG_ERROR
    )
    assert "malformed run id" in capsys.readouterr().err


def test_an_unexpected_exception_is_internal_not_fail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Python exits 1 on an unhandled exception, and 1 is `fail`: CI would read a crash as
    # a run that measured something and did not pass.
    def crash(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr(commands, "connect", lambda **_k: FakeCluster())
    monkeypatch.setattr(commands, "submit", crash)

    assert main(["submit", str(EXAMPLE)]) == ExitCode.INTERNAL
    err = capsys.readouterr().err
    assert "internal error (RuntimeError: something nobody anticipated)" in err
    assert "Traceback" in err


def test_an_interrupt_is_still_not_swallowed_by_the_catch_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # KeyboardInterrupt is not an Exception, so Ctrl-C outside the log stream still stops
    # the CLI rather than being reported as an internal error.
    def interrupt(*_args: object, **_kwargs: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(commands, "connect", lambda **_k: FakeCluster())
    monkeypatch.setattr(commands, "submit", interrupt)

    with pytest.raises(KeyboardInterrupt):
        main(["submit", str(EXAMPLE)])


def test_a_cluster_error_carries_its_notes_to_the_user(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(commands, "connect", lambda **_k: FakeCluster(fail_on_kind="Job"))

    assert main(["submit", str(EXAMPLE)]) == ExitCode.INTERNAL
    assert "422" in capsys.readouterr().err


def test_the_in_cluster_command_is_not_advertised(capsys: pytest.CaptureFixture[str]) -> None:
    # `execute` is the Job's entry point, not something a person runs. It still works
    # when named, but listing it invites someone to run a phase-2-only orchestrator by
    # hand and wonder why it exits 5.
    with pytest.raises(SystemExit):
        main(["--help"])

    listed = capsys.readouterr().out
    commands = [line.strip().split()[0] for line in listed.splitlines() if line.startswith("    ")]
    assert "validate" in commands
    assert "execute" not in commands
    assert "SUPPRESS" not in listed
