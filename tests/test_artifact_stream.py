"""A run's artifacts, through the Job's log and back (ADR-05-15, provisional)."""

from __future__ import annotations

import base64
import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from dokimi.artifact_stream import ArtifactReceiver, ArtifactStreamError, write_artifacts

RUN_ID = "20261002-120000-a1b2c3"


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    root = tmp_path / RUN_ID
    (root / "pairs").mkdir(parents=True)
    (root / "suite.robot").write_text("*** Settings ***\n", encoding="utf-8")
    (root / "log.html").write_text("<html>log</html>" * 2000, encoding="utf-8")
    (root / "pairs" / "pair-001.xml").write_text("<robot/>", encoding="utf-8")
    return root


def streamed(directory: Path, run_id: str = RUN_ID) -> list[str]:
    lines: list[str] = []
    write_artifacts(directory, run_id, lines.append)
    return lines


def test_the_artifacts_come_back_exactly_as_the_job_wrote_them(
    directory: Path, tmp_path: Path
) -> None:
    receiver = ArtifactReceiver(RUN_ID)
    for line in streamed(directory):
        receiver.feed(line)

    out = receiver.extract(tmp_path / "results" / RUN_ID)

    assert receiver.received
    for path in directory.rglob("*"):
        if path.is_file():
            assert (out / path.relative_to(directory)).read_bytes() == path.read_bytes()


def test_the_block_never_reaches_the_screen_and_the_rest_of_the_log_does(
    directory: Path,
) -> None:
    receiver = ArtifactReceiver(RUN_ID)
    log = ["phase 9a (reporting): ...", *streamed(directory), "run finished"]

    printed = [line for line in log if receiver.feed(line) is not None]

    assert printed == ["phase 9a (reporting): ...", "run finished"]


def test_the_lines_stay_short(directory: Path) -> None:
    # A log is read line by line; one enormous line would be split by the runtime.
    assert max(len(line) for line in streamed(directory)[1:-1]) <= 76


def test_another_runs_block_is_just_log_lines(directory: Path) -> None:
    receiver = ArtifactReceiver(RUN_ID)
    lines = streamed(directory, run_id="20261002-110000-zzzzzz")

    assert [line for line in lines if receiver.feed(line) is not None] == lines
    assert not receiver.received


def test_an_altered_block_is_refused_and_nothing_is_extracted(
    directory: Path, tmp_path: Path
) -> None:
    lines = streamed(directory)
    lines[2] = ("A" if lines[2][0] != "A" else "B") + lines[2][1:]
    receiver = ArtifactReceiver(RUN_ID)
    for line in lines:
        receiver.feed(line)

    assert not receiver.received
    assert receiver.error is not None
    with pytest.raises(ArtifactStreamError, match="truncated or altered"):
        receiver.extract(tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_a_stream_that_stops_inside_the_block_says_so(directory: Path) -> None:
    receiver = ArtifactReceiver(RUN_ID)
    for line in streamed(directory)[:-3]:
        receiver.feed(line)

    assert receiver.incomplete
    assert not receiver.received


def test_an_archive_that_reaches_outside_its_directory_is_refused(tmp_path: Path) -> None:
    # Checksummed by whoever made it, so the checksum is no protection: extraction is.
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo("../escaped.txt")
        member.size = 3
        archive.addfile(member, io.BytesIO(b"bad"))
    data = buffer.getvalue()
    encoded = base64.b64encode(data).decode()
    receiver = ArtifactReceiver(RUN_ID)
    for line in [
        f"::dokimi-artifacts begin run={RUN_ID} bytes={len(data)} "
        f"sha256={hashlib.sha256(data).hexdigest()}::",
        encoded,
        f"::dokimi-artifacts end run={RUN_ID}::",
    ]:
        receiver.feed(line)

    with pytest.raises(ArtifactStreamError, match="refusing to extract"):
        receiver.extract(tmp_path / "out")
    assert not (tmp_path / "escaped.txt").exists()
