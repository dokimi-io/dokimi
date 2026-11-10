"""Shell completion: ``dokimi completion bash|zsh|fish`` prints a script for that shell.

The scripts are generated from the CLI's own parser, so a command or flag added there is
completed without anyone editing a script, and a test loads each script in its shell to
keep it that way. They are static: they complete commands, flags, fixed values and files
from the shell alone, with no Python start-up on each tab press, and nothing that only a
cluster knows, such as run ids.

Every option that takes a value needs a decision on what to offer for it, made here once:
files, directories, definitions, image components, or nothing. An option nobody decided
for is refused when the script is generated, so it can't ship offering the wrong thing.
"""

from __future__ import annotations

import argparse
import enum
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from dokimi.run_images import COMPONENTS

__all__ = ["SHELLS", "Argument", "Command", "UnclassifiedArgument", "commands", "script"]

#: The shells there is a script for. Native Windows isn't supported, so no PowerShell.
SHELLS = ("bash", "zsh", "fish")


class Values(enum.Enum):
    """What a value is completed with."""

    NONE = "none"
    """Something only the user or the cluster knows: nothing is offered, not even files."""
    CHOICES = "choices"
    FILE = "file"
    DIRECTORY = "directory"
    DEFINITION = "definition"
    """A test definition: ``.yaml``, ``.yml`` or ``.robot`` files, and directories."""
    IMAGE = "image"
    """``--image``'s ``COMPONENT=``, with the reference left to the user."""


#: Values completed with something, by the argument's ``dest``.
_COMPLETED = {
    "definition": Values.DEFINITION,
    "kubeconfig": Values.FILE,
    "results_directory": Values.DIRECTORY,
    "images": Values.IMAGE,
}

#: Values only the user or the cluster knows. Offering files for them would be wrong.
_FREE = frozenset(
    {
        "run_id",
        "namespace",
        "context",
        "service_account",
        "older_than",
        "cluster_name",
        "image_override",
    }
)

#: Definition files, by suffix.
_DEFINITION_SUFFIXES = ("yaml", "yml", "robot")


class UnclassifiedArgument(LookupError):
    """An option takes a value, and nobody decided what to complete it with."""


@dataclass(frozen=True)
class Argument:
    """One flag, with its aliases, or one positional argument."""

    flags: tuple[str, ...]
    """Empty for a positional argument."""
    name: str
    help: str
    takes_value: bool
    values: Values
    choices: tuple[str, ...] = ()
    repeatable: bool = False
    optional: bool = False
    """For a positional argument: it may be left out."""
    ends_command: bool = False
    """``--help`` and ``--version``: nothing else follows them."""


@dataclass(frozen=True)
class Command:
    """A command and its arguments. The program itself is the command named ``""``."""

    name: str
    help: str
    arguments: tuple[Argument, ...]

    @property
    def flags(self) -> tuple[Argument, ...]:
        return tuple(argument for argument in self.arguments if argument.flags)

    @property
    def positionals(self) -> tuple[Argument, ...]:
        return tuple(argument for argument in self.arguments if not argument.flags)


def commands(parser: argparse.ArgumentParser) -> tuple[Command, ...]:
    """The program and every listed command, in the order ``--help`` lists them.

    A command added without ``help`` is unlisted, as ``execute`` is, and isn't completed.
    """
    program = Command("", "", _arguments(parser))
    listed: list[Command] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for choice in action._choices_actions:
                subparser = action.choices[choice.dest]
                listed.append(Command(choice.dest, _one_line(choice.help), _arguments(subparser)))
    return (program, *listed)


def script(shell: str, parser: argparse.ArgumentParser) -> str:
    """The completion script for ``shell``, one of :data:`SHELLS`."""
    renderers: dict[str, Callable[[Sequence[Command]], str]] = {
        "bash": _bash,
        "zsh": _zsh,
        "fish": _fish,
    }
    return renderers[shell](commands(parser))


def _arguments(parser: argparse.ArgumentParser) -> tuple[Argument, ...]:
    return tuple(
        _argument(action)
        for action in parser._actions
        if not isinstance(action, argparse._SubParsersAction)
    )


def _argument(action: argparse.Action) -> Argument:
    takes_value = action.nargs != 0
    choices = tuple(str(choice) for choice in action.choices or ())
    if not takes_value:
        values = Values.NONE
    elif choices:
        values = Values.CHOICES
    elif action.dest in _COMPLETED:
        values = _COMPLETED[action.dest]
    elif action.dest in _FREE:
        values = Values.NONE
    else:
        flag = action.option_strings[-1] if action.option_strings else action.dest
        raise UnclassifiedArgument(
            f"{flag} takes a value, and shell completion doesn't know what to offer for it. "
            f"Add {action.dest!r} to _COMPLETED or _FREE in {__name__}."
        )
    name = action.metavar if isinstance(action.metavar, str) else action.dest
    return Argument(
        flags=tuple(action.option_strings),
        name=name.lower().replace("_", "-"),
        help=_one_line(action.help),
        takes_value=takes_value,
        values=values,
        choices=choices,
        repeatable=isinstance(action, argparse._AppendAction),
        optional=not action.option_strings and action.nargs == "?",
        ends_command=isinstance(action, argparse._HelpAction | argparse._VersionAction),
    )


def _one_line(text: str | None) -> str:
    if text is None or text == argparse.SUPPRESS:
        return ""
    return " ".join(text.split())


def _image_prefixes() -> tuple[str, ...]:
    return tuple(f"{component}=" for component in sorted(COMPONENTS))


# --- bash -----------------------------------------------------------------------------
#
# Written for bash 3.2 as well, the version macOS still ships: no `mapfile`, and `compopt`
# only where it exists. Loaded with `eval`, because bash 3.2's `source <(...)` silently
# reads nothing. `=` is one of bash's word breaks, so `--to=yaml` reaches the
# function as `--to`, `=`, `yaml`.

_BASH_HEAD = """\
# bash completion for dokimi, printed by `dokimi completion bash`.
# Load it in the current shell with
#   eval "$(dokimi completion bash)"
# or save it as "dokimi" in bash-completion's completions directory.

# One completion per line on stdin, into COMPREPLY. A file name may contain spaces.
_dokimi_reply() {
    local line
    COMPREPLY=()
    while IFS= read -r line; do
        COMPREPLY+=("$line")
    done
}

# Quote what is offered as a file name, and end a directory with a slash. Bash 4 and
# newer only; on bash 3.2 a directory is offered without its slash.
_dokimi_filenames() {
    if type compopt >/dev/null 2>&1; then
        compopt -o filenames
    fi
    return 0
}

_dokimi_words() {
    _dokimi_reply < <(compgen -W "$1" -- "$cur")
}

_dokimi_files() {
    _dokimi_reply < <(compgen -f -- "$cur")
    _dokimi_filenames
}

_dokimi_directories() {
    _dokimi_reply < <(compgen -d -- "$cur")
    _dokimi_filenames
}

_dokimi_definitions() {
    _dokimi_reply < <(
        compgen -d -- "$cur"
__DEFINITION_GLOBS__
    )
    _dokimi_filenames
}

_dokimi_images() {
    _dokimi_words "__IMAGES__"
    if type compopt >/dev/null 2>&1; then
        compopt -o nospace
    fi
    return 0
}

_dokimi() {
    local cur="${COMP_WORDS[COMP_CWORD]}" prev="" command="" i
    if ((COMP_CWORD > 0)); then
        prev="${COMP_WORDS[COMP_CWORD - 1]}"
    fi
    if [[ "$cur" == "=" ]]; then
        cur=""
    elif [[ "$prev" == "=" ]]; then
        prev=""
        if ((COMP_CWORD > 1)); then
            prev="${COMP_WORDS[COMP_CWORD - 2]}"
        fi
        # After `component=` in --image: the reference is the user's to type.
        if [[ "$prev" != -* ]]; then
            return 0
        fi
    fi
    for ((i = 1; i < COMP_CWORD; i++)); do
        case "${COMP_WORDS[i]}" in
            -*) ;;
            *)
                command="${COMP_WORDS[i]}"
                break
                ;;
        esac
    done

    case "$command" in
"""

_BASH_TAIL = """\
    esac
    return 0
}

complete -F _dokimi dokimi
"""


def _bash(commands: Sequence[Command]) -> str:
    globs = "\n".join(
        f"        compgen -f -X '!*.{suffix}' -- \"$cur\"" for suffix in _DEFINITION_SUFFIXES
    )
    head = _BASH_HEAD.replace("__DEFINITION_GLOBS__", globs).replace(
        "__IMAGES__", " ".join(_image_prefixes())
    )
    program, *listed = commands
    cases = [_bash_case('""', program, " ".join(command.name for command in listed))]
    cases.extend(_bash_case(command.name, command) for command in listed)
    return head + "".join(cases) + _BASH_TAIL


def _bash_case(pattern: str, command: Command, words: str = "") -> str:
    lines = [f"        {pattern})"]
    valued = [argument for argument in command.flags if argument.takes_value]
    # Every value nothing is offered for shares one branch: it only has to stop the flags
    # and files below from being offered as a value.
    free = [flag for argument in valued if not _bash_reply(argument) for flag in argument.flags]
    if valued:
        lines.append('            case "$prev" in')
        for argument in valued:
            reply = _bash_reply(argument)
            if reply:
                lines.append(f"                {'|'.join(argument.flags)})")
                lines.append(f"                    {reply}")
                lines.append("                    return 0")
                lines.append("                    ;;")
        if free:
            lines.append(f"                {'|'.join(free)})")
            lines.append("                    return 0")
            lines.append("                    ;;")
        lines.append("            esac")
    flags = " ".join(flag for argument in command.flags for flag in argument.flags)
    positional = next(iter(command.positionals), None)
    otherwise = f'_dokimi_words "{words}"' if words else _bash_reply(positional)
    lines.append('            if [[ "$cur" == -* ]]; then')
    lines.append(f'                _dokimi_words "{flags}"')
    if otherwise:
        lines.append("            else")
        lines.append(f"                {otherwise}")
    lines.append("            fi")
    lines.append("            ;;")
    return "\n".join(lines) + "\n"


def _bash_reply(argument: Argument | None) -> str:
    if argument is None:
        return ""
    return {
        Values.NONE: "",
        Values.CHOICES: f'_dokimi_words "{" ".join(argument.choices)}"',
        Values.FILE: "_dokimi_files",
        Values.DIRECTORY: "_dokimi_directories",
        Values.DEFINITION: "_dokimi_definitions",
        Values.IMAGE: "_dokimi_images",
    }[argument.values]


# --- zsh ------------------------------------------------------------------------------
#
# The usual `_arguments` form. The last lines make the file work both ways: autoloaded
# from $fpath, where zsh runs it as the body of `_dokimi`, and sourced, where it
# registers itself with `compdef`.

_ZSH_TAIL = """\
_dokimi_images() {
  compadd -S '' -- __IMAGES__
}

if [ "$funcstack[1]" = "_dokimi" ]; then
  _dokimi "$@"
else
  compdef _dokimi dokimi
fi
"""


def _zsh(commands: Sequence[Command]) -> str:
    program, *listed = commands
    lines = [
        "#compdef dokimi",
        "# zsh completion for dokimi, printed by `dokimi completion zsh`.",
        "# Load it in the current shell with",
        "#   source <(dokimi completion zsh)",
        '# or save it as "_dokimi" in a directory on $fpath, before compinit runs.',
        "",
        "_dokimi() {",
        "  local curcontext=$curcontext state state_descr line",
        "  typeset -A opt_args",
        "",
        "  _arguments -C \\",
        *(f"    {_zsh_spec(argument)} \\" for argument in program.flags),
        "    '1: :_dokimi_commands' \\",
        "    '*:: :->command' && return",
        "",
        "  case $state in",
        "    command)",
        '      curcontext="${curcontext%:*:*}:dokimi-$words[1]:"',
        "      case $words[1] in",
    ]
    for command in listed:
        specs = [_zsh_spec(argument) for argument in command.arguments]
        lines.append(f"        {command.name})")
        lines.append("          _arguments \\")
        lines.extend(f"            {spec} \\" for spec in specs[:-1])
        lines.append(f"            {specs[-1]}")
        lines.append("          ;;")
    lines += [
        "      esac",
        "      ;;",
        "  esac",
        "}",
        "",
        "_dokimi_commands() {",
        "  local -a commands=(",
        *(f"    {_zsh_quote(f'{command.name}:{command.help}')}" for command in listed),
        "  )",
        "  _describe -t commands 'dokimi command' commands",
        "}",
        "",
    ]
    return "\n".join(lines) + "\n" + _ZSH_TAIL.replace("__IMAGES__", " ".join(_image_prefixes()))


def _zsh_spec(argument: Argument) -> str:
    action = {
        Values.NONE: " ",
        Values.CHOICES: f"({' '.join(argument.choices)})",
        Values.FILE: "_files",
        Values.DIRECTORY: "_files -/",
        Values.DEFINITION: f'_files -g "*.({"|".join(_DEFINITION_SUFFIXES)})(-.)"',
        Values.IMAGE: "_dokimi_images",
    }[argument.values]
    if not argument.flags:
        colons = "::" if argument.optional else ":"
        return _zsh_quote(f"{colons}{argument.name}:{action}")
    description = argument.help
    for special in ("\\", "[", "]"):
        description = description.replace(special, "\\" + special)
    value = f":{argument.name}:{action}" if argument.takes_value else ""
    body = f"[{description}]{value}"
    if argument.ends_command:
        exclusions = "(- *)"
    elif len(argument.flags) > 1:
        exclusions = f"({' '.join(argument.flags)})"
    else:
        exclusions = ""
    if argument.repeatable:
        exclusions = "*"
    if len(argument.flags) == 1:
        return _zsh_quote(exclusions + argument.flags[0] + body)
    return f"{_zsh_quote(exclusions)}{{{','.join(argument.flags)}}}{_zsh_quote(body)}"


def _zsh_quote(text: str) -> str:
    if not text:
        return ""
    return "'" + text.replace("'", "'\\''") + "'"


# --- fish -----------------------------------------------------------------------------
#
# Files are offered only where an argument asks for them (`complete -f` turns them off
# for everything else). Definitions have a function of their own: fish's
# `__fish_complete_suffix` puts matching files first, but offers every other file too.

_FISH_DEFINITIONS = """\
function __dokimi_definitions
    # The word as typed, unescaped, so that `with\\ sp` finds "with space.yaml".
    set -l token (commandline -ct | string unescape)
    for path in "$token"*
        if test -d $path
            printf '%s/\\n' $path
        else if string match -qr '\\.(__SUFFIXES__)$' -- $path
            printf '%s\\n' $path
        end
    end
end
"""


def _fish(commands: Sequence[Command]) -> str:
    program, *listed = commands
    lines = [
        "# fish completion for dokimi, printed by `dokimi completion fish`.",
        "# Load it in the current shell with",
        "#   dokimi completion fish | source",
        "# or save it as ~/.config/fish/completions/dokimi.fish.",
        "",
        _FISH_DEFINITIONS.replace("__SUFFIXES__", "|".join(_DEFINITION_SUFFIXES)),
        "complete -c dokimi -f",
    ]
    for argument in program.flags:
        lines.append(_fish_line("__fish_use_subcommand", argument))
    for command in listed:
        lines.append(
            f"complete -c dokimi -n __fish_use_subcommand -a {command.name} "
            f"-d {_fish_quote(command.help)}"
        )
    for command in listed:
        condition = _fish_quote(f"__fish_seen_subcommand_from {command.name}")
        lines.extend(_fish_line(condition, argument) for argument in command.flags)
        lines.extend(
            f"complete -c dokimi -n {condition}{_fish_values(positional)}"
            for positional in command.positionals
            if positional.values is not Values.NONE
        )
    return "\n".join(lines) + "\n"


def _fish_line(condition: str, argument: Argument) -> str:
    names = " ".join(
        f"-l {flag[2:]}" if flag.startswith("--") else f"-s {flag[1:]}" for flag in argument.flags
    )
    values = _fish_values(argument) if argument.takes_value else ""
    return f"complete -c dokimi -n {condition} {names}{values} -d {_fish_quote(argument.help)}"


def _fish_values(argument: Argument) -> str:
    """What follows the names: whether a value is required, and what to offer for it."""
    return {
        Values.NONE: " -x",
        Values.CHOICES: f" -x -a {_fish_quote(' '.join(argument.choices))}",
        Values.FILE: " -r -F",
        Values.DIRECTORY: " -x -a '(__fish_complete_directories (commandline -ct))'",
        Values.DEFINITION: " -x -a '(__dokimi_definitions)'",
        Values.IMAGE: f" -x -a {_fish_quote(' '.join(_image_prefixes()))}",
    }[argument.values]


def _fish_quote(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"
