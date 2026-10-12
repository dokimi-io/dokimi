"""A run's artifacts, through the Job's log and back (ADR-05-15, provisional)."""

from __future__ import annotations

import base64
import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from dokimi.artifact_stream import (
    FORMAT_VERSION,
    ArtifactReceiver,
    ArtifactStreamError,
    write_artifacts,
)

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

    printed = [shown for line in log for shown in receiver.feed(line)]

    assert printed == ["phase 9a (reporting): ...", "run finished"]


def test_the_lines_stay_short(directory: Path) -> None:
    # A log is read line by line; one enormous line would be split by the runtime.
    assert max(len(line) for line in streamed(directory)[1:-1]) <= 76


def test_another_runs_block_is_just_log_lines(directory: Path) -> None:
    receiver = ArtifactReceiver(RUN_ID)
    lines = streamed(directory, run_id="20261002-110000-zzzzzz")

    assert [shown for line in lines for shown in receiver.feed(line)] == lines
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


def block(data: bytes, version: int = FORMAT_VERSION) -> list[str]:
    """A block around *data*, checksummed as the Job would, whatever *data* holds."""
    encoded = base64.b64encode(data).decode()
    return [
        f"::dokimi-artifacts begin v={version} run={RUN_ID} bytes={len(data)} "
        f"sha256={hashlib.sha256(data).hexdigest()}::",
        *(encoded[start : start + 76] for start in range(0, len(encoded), 76)),
        f"::dokimi-artifacts end run={RUN_ID}::",
    ]


def archive_of(*members: tuple[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in members:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


def received(lines: list[str]) -> ArtifactReceiver:
    receiver = ArtifactReceiver(RUN_ID)
    for line in lines:
        receiver.feed(line)
    return receiver


def test_an_archive_that_reaches_outside_its_directory_is_refused(tmp_path: Path) -> None:
    # Checksummed by whoever made it, so the checksum is no protection: extraction is.
    receiver = received(block(archive_of(("../escaped.txt", b"bad"))))

    with pytest.raises(ArtifactStreamError, match="refusing to extract"):
        receiver.extract(tmp_path / "out")
    assert not (tmp_path / "escaped.txt").exists()


def test_nothing_is_extracted_when_any_member_is_refused(tmp_path: Path) -> None:
    # extractall() filters member by member, so the good one would already be written.
    receiver = received(block(archive_of(("log.html", b"<html/>"), ("../escaped.txt", b"bad"))))

    with pytest.raises(ArtifactStreamError, match="refusing to extract"):
        receiver.extract(tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_a_damaged_archive_with_a_good_checksum_is_an_artifact_error(tmp_path: Path) -> None:
    # The checksum matches what was sent; what was sent is not a readable archive.
    receiver = received(block(b"\x1f\x8b" + b"not a gzip stream at all"))

    assert receiver.received
    with pytest.raises(ArtifactStreamError, match="cannot be read"):
        receiver.extract(tmp_path / "out")


def test_a_framing_version_this_cli_does_not_read_is_reported(directory: Path) -> None:
    lines = streamed(directory)
    lines[0] = lines[0].replace(f"v={FORMAT_VERSION} ", "v=99 ")
    receiver = received(lines)

    assert not receiver.received
    assert "framing version 99" in str(receiver.error)


def test_a_marker_in_robots_output_does_not_swallow_the_log(directory: Path) -> None:
    # The Job's log is also Robot's console output, so a begin marker can appear early.
    fake = streamed(directory)[0]
    receiver = ArtifactReceiver(RUN_ID)
    log = [fake, "pair-001 | FAIL |", *streamed(directory), "run finished"]

    printed = [shown for line in log for shown in receiver.feed(line)]

    # The ordinary line is printed at once; the false begin only when the real one shows.
    assert printed == ["pair-001 | FAIL |", fake, "run finished"]
    assert receiver.received


def test_a_line_from_the_other_stream_inside_the_block_is_printed_and_the_block_survives(
    directory: Path, tmp_path: Path
) -> None:
    # Found on kind: stdout and stderr are read separately, so the Job's note about the
    # block, written after it to stderr, arrived 2,281 lines into it.
    lines = streamed(directory)
    note = "artifacts: 148066 bytes in the log above, for submit and attach to extract"
    receiver = ArtifactReceiver(RUN_ID)

    log = [*lines[:5], note, *lines[5:]]
    printed = [shown for line in log for shown in receiver.feed(line)]

    assert printed == [note]
    assert (receiver.extract(tmp_path / "out") / "suite.robot").is_file()


def test_a_second_begin_starts_over_and_the_last_block_counts(
    directory: Path, tmp_path: Path
) -> None:
    lines = streamed(directory)
    receiver = ArtifactReceiver(RUN_ID)

    printed = [shown for line in [lines[0], lines[1], *lines] for shown in receiver.feed(line)]

    assert printed == lines[:2]
    assert (receiver.extract(tmp_path / "out") / "suite.robot").is_file()
