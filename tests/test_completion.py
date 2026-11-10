"""Shell completion: the scripts follow the parser, and each one completes in its shell.

The scripts are generated, so the first half checks the generator against the parser it
reads. The second half loads each script in a real bash, zsh and fish, and asks for
completions the way a tab press would. A shell that isn't installed skips its tests
locally, and fails them in CI, which installs all three.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from dokimi.cli import completion
from dokimi.cli.exit_codes import ExitCode
from dokimi.cli.main import build_parser, main
from dokimi.run_images import COMPONENTS

LISTED = ("validate", "convert", "submit", "preflight", "attach", "cleanup", "image", "completion")
IMAGE_PREFIXES = sorted(f"{component}=" for component in COMPONENTS)


def _shell(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        if os.environ.get("CI"):
            pytest.fail(f"{name} is not installed, and CI installs it to run these tests")
        pytest.skip(f"{name} is not installed")
    return path


@pytest.fixture
def definitions(tmp_path: Path) -> Path:
    """A directory with definitions of each kind, a file that isn't one, and a directory."""
    for name in ("a.yaml", "b.yml", "c.robot", "notes.txt", "with space.yaml"):
        (tmp_path / name).touch()
    (tmp_path / "suites").mkdir()
    return tmp_path


def _write_script(shell: str, directory: Path) -> Path:
    path = directory / f"dokimi.{shell}"
    path.write_text(completion.script(shell, build_parser()))
    return path


# --- the generator ---------------------------------------------------------------------


def test_every_listed_command_is_completed_and_execute_never_is() -> None:
    names = [command.name for command in completion.commands(build_parser())]

    assert names == ["", *LISTED]


@pytest.mark.parametrize("shell", completion.SHELLS)
def test_every_flag_of_every_command_is_in_the_script(shell: str) -> None:
    text = completion.script(shell, build_parser())

    for command in completion.commands(build_parser()):
        for argument in command.flags:
            for flag in argument.flags:
                spelled = f"-l {flag[2:]}" if shell == "fish" and flag.startswith("--") else flag
                if shell == "fish" and not flag.startswith("--"):
                    spelled = f"-s {flag[1:]}"
                assert spelled in text, (command.name, flag)


@pytest.mark.parametrize("shell", completion.SHELLS)
def test_the_internal_execute_command_is_in_no_script(shell: str) -> None:
    text = completion.script(shell, build_parser())

    assert re.search(r"\bexecute\b", text) is None
    assert "--deadline-seconds" not in text


def test_an_option_nobody_decided_completion_for_is_refused() -> None:
    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    subparsers.choices["submit"].add_argument("--new-thing")

    with pytest.raises(completion.UnclassifiedArgument, match="--new-thing"):
        completion.commands(parser)


def test_a_flag_with_choices_needs_no_decision() -> None:
    commands = {command.name: command for command in completion.commands(build_parser())}
    retain = next(argument for argument in commands["submit"].flags if "--retain" in argument.flags)

    assert retain.values is completion.Values.CHOICES
    assert retain.choices == ("on-failure", "always", "never")


@pytest.mark.parametrize("shell", completion.SHELLS)
def test_completion_prints_the_script_and_exits_0(
    shell: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["completion", shell]) == ExitCode.PASS

    assert capsys.readouterr().out == completion.script(shell, build_parser())


def test_an_unknown_shell_is_a_config_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        main(["completion", "powershell"])

    assert exit_.value.code == ExitCode.CONFIG_ERROR
    assert "invalid choice: 'powershell'" in capsys.readouterr().err


# --- bash ------------------------------------------------------------------------------
#
# The completion function is called as bash calls it: COMP_WORDS split at bash's word
# breaks, `=` among them, and COMP_CWORD the word being completed.

_BASH_DRIVER = """\
source "$1"
shift
COMP_WORDS=("$@")
COMP_CWORD=$((${#COMP_WORDS[@]} - 1))
_dokimi
if ((${#COMPREPLY[@]})); then
    printf '%s\\n' "${COMPREPLY[@]}"
fi
"""


def _bash(script: Path, directory: Path, *words: str) -> list[str]:
    result = subprocess.run(
        [_shell("bash"), "--norc", "--noprofile", "-c", _BASH_DRIVER, "bash", str(script), *words],
        cwd=directory,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return sorted(result.stdout.splitlines())


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (["dokimi", ""], sorted(LISTED)),
        (["dokimi", "su"], ["submit"]),
        (["dokimi", "-"], ["--help", "--version", "-V", "-h"]),
        (["dokimi", "submit", "--re"], ["--retain"]),
        (["dokimi", "submit", "--retain", ""], ["always", "never", "on-failure"]),
        (["dokimi", "submit", "--retain", "=", "al"], ["always"]),
        (["dokimi", "convert", "--to", ""], ["robot", "yaml"]),
        (["dokimi", "submit", "--image", ""], IMAGE_PREFIXES),
        (["dokimi", "submit", "--image", "orchestrator", "=", ""], []),
        (["dokimi", "attach", "-n", ""], []),
        (["dokimi", "attach", "--results-directory", ""], ["suites"]),
        (["dokimi", "cleanup", ""], []),
        (["dokimi", "completion", ""], ["bash", "fish", "zsh"]),
        (["dokimi", "ex"], []),
    ],
)
def test_bash_completes(words: list[str], expected: list[str], definitions: Path) -> None:
    script = _write_script("bash", definitions)

    assert _bash(script, definitions, *words) == expected


@pytest.mark.parametrize("command", ["validate", "convert", "submit", "preflight"])
def test_bash_offers_definitions_and_directories_only(command: str, definitions: Path) -> None:
    script = _write_script("bash", definitions)

    offered = _bash(script, definitions, "dokimi", command, "")

    assert offered == ["a.yaml", "b.yml", "c.robot", "suites", "with space.yaml"]


# --- zsh -------------------------------------------------------------------------------
#
# zsh completes only inside its line editor, so the driver starts an interactive zsh on a
# pseudo-terminal, types each line and a tab, and reports what was added as a match.
# `compadd` is wrapped to print every match, marked, before adding it as usual.

_ZSH_DRIVER = """\
zmodload zsh/zpty
zpty dokimi zsh -f -i
output=""
read_until() {
    local line
    output=""
    while [[ $output != *$1* ]]; do
        if ! zpty -r -t dokimi line; then
            sleep 0.01
            (( SECONDS < 30 )) || { print -u2 "zsh never printed $1"; exit 1 }
        fi
        output+=$line
    done
}
zpty -w dokimi "cd ${(q)2}; PROMPT='<prompt>'; RPROMPT=''"
zpty -w dokimi 'autoload -Uz compinit && compinit -u -D'
zpty -w dokimi "source ${(q)1}"
zpty -w dokimi 'compadd() {
    local -a matches
    builtin compadd -O matches "$@"
    print -rn -- "${(j::)${(@)matches/#/<match>}}<end>"
    builtin compadd "$@"
}'
zpty -w dokimi 'print READY""'
read_until READY
# Keys typed before the line editor is back are echoed, not completed.
read_until '<prompt>'
shift 2
for line in "$@"; do
    zpty -n -w dokimi "$line"$'\\t'
    zpty -n -w dokimi $'\\C-u''print DONE""'$'\\n'
    read_until DONE
    print -r -- "$output"
    print -- "<line>"
    read_until '<prompt>'
done
zpty -d dokimi
"""


def _zsh(script: Path, directory: Path, *lines: str) -> list[list[str]]:
    result = subprocess.run(
        [_shell("zsh"), "-f", "-c", _ZSH_DRIVER, "zsh", str(script), str(directory), *lines],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    outputs = result.stdout.split("<line>")[: len(lines)]
    return [
        sorted(
            {
                match
                for chunk in re.findall(r"<match>(.*?)<end>", output)
                for match in chunk.split("<match>")
            }
            - {""}
        )
        for output in outputs
    ]


def test_zsh_completes(definitions: Path) -> None:
    script = _write_script("zsh", definitions)
    cases = {
        "dokimi ": sorted(LISTED),
        "dokimi su": ["submit"],
        "dokimi submit --re": ["--retain"],
        "dokimi submit --retain ": ["always", "never", "on-failure"],
        "dokimi convert --to ": ["robot", "yaml"],
        "dokimi submit --image ": IMAGE_PREFIXES,
        "dokimi attach -n ": [],
        "dokimi completion ": ["bash", "fish", "zsh"],
        # As zsh adds them: quoted for the line, and a directory's slash added afterwards.
        "dokimi submit ": ["a.yaml", "b.yml", "c.robot", "suites", "with\\ space.yaml"],
    }

    offered = _zsh(script, definitions, *cases)

    assert dict(zip(cases, offered, strict=True)) == cases


def _zsh_completer(*commands: str) -> str:
    """What zsh completes ``dokimi`` with, after ``compinit`` and then ``commands``."""
    result = subprocess.run(
        [_shell("zsh"), "-f", "-c", "; ".join((*commands, "print -r -- $_comps[dokimi]"))],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout.strip()


def test_zsh_loads_the_script_when_sourced(tmp_path: Path) -> None:
    script = _write_script("zsh", tmp_path)

    assert _zsh_completer("autoload -Uz compinit", "compinit -u -D", f"source {script}") == (
        "_dokimi"
    )


def test_zsh_loads_the_script_from_fpath(tmp_path: Path) -> None:
    functions = tmp_path / "functions"
    functions.mkdir()
    (functions / "_dokimi").write_text(completion.script("zsh", build_parser()))

    assert _zsh_completer(
        f"fpath=({functions} $fpath)", "autoload -Uz compinit", "compinit -u -D"
    ) == ("_dokimi")


# --- fish ------------------------------------------------------------------------------
#
# `complete -C` asks fish for the completions of a command line, exactly as a tab would.
# fish 4 also returns fuzzy matches, `--orchestrator-image` for `--re`, and ranks them after
# the ones that start with the word typed. Only those first ones are what the script offers,
# and the same in fish 3, which has no fuzzy matches.


def _fish(script: Path, directory: Path, line: str) -> list[str]:
    result = subprocess.run(
        [_shell("fish"), "--no-config", "-c", f"source {script}; complete -C {_fish_quote(line)}"],
        cwd=directory,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    # The last word, where `\ ` is an escaped space inside it, as fish reads it.
    typed = re.split(r"(?<!\\) ", line)[-1].replace("\\ ", " ")
    candidates = (entry.split("\t")[0] for entry in result.stdout.splitlines())
    return sorted(candidate for candidate in candidates if candidate.startswith(typed))


def _fish_quote(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("dokimi ", sorted(LISTED)),
        ("dokimi su", ["submit"]),
        ("dokimi submit --re", ["--retain"]),
        ("dokimi submit --retain ", ["always", "never", "on-failure"]),
        ("dokimi submit --retain=", ["--retain=always", "--retain=never", "--retain=on-failure"]),
        ("dokimi convert --to ", ["robot", "yaml"]),
        ("dokimi submit --image ", IMAGE_PREFIXES),
        ("dokimi attach -n ", []),
        ("dokimi completion ", ["bash", "fish", "zsh"]),
        ("dokimi submit ", ["a.yaml", "b.yml", "c.robot", "suites/", "with space.yaml"]),
        ("dokimi submit with\\ sp", ["with space.yaml"]),
    ],
)
def test_fish_completes(line: str, expected: list[str], definitions: Path) -> None:
    script = _write_script("fish", definitions)

    assert _fish(script, definitions, line) == expected
