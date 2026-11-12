
"""Parse the official PS-035 commands.txt into typed command records.

This module reads and validates command files. It never executes commands
or writes to files.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ps035.loader import p_date, p_id


# --------------------------------------------------------------------------
# Public records
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Command:
    """One parsed command line."""

    name: str
    args: tuple[str, ...]
    line_number: int
    raw_line: str


@dataclass(frozen=True)
class CommandIssue:
    """One problem found in a command file."""

    file: str
    line: int | None
    message: str

    def __str__(self) -> str:
        where = f"{self.file} line {self.line}" if self.line else self.file
        return f"{where}: {self.message}"


class CommandParseError(Exception):
    """Raised with all problems found in a command file."""

    def __init__(self, issues: list[CommandIssue]):
        self.issues = issues
        shown = "\n".join(f"  - {issue}" for issue in issues[:25])
        more = (
            f"\n  ... and {len(issues) - 25} more"
            if len(issues) > 25
            else ""
        )
        super().__init__(
            f"{len(issues)} command file issue(s):\n{shown}{more}"
        )


# --------------------------------------------------------------------------
# Command specifications
# --------------------------------------------------------------------------

_Validator = Callable[[str], object]

COMMAND_SPECS: dict[str, tuple[tuple[str, _Validator], ...]] = {
    "PLAN": (("date", p_date),),
    "PRINT_ROUTE": (
        ("rider_id", p_id("RDR")),
        ("date", p_date),
    ),
    "LOAD_EVENTS": (("date", p_date),),
    "LOAD_ODOMETER": (("date", p_date),),
    "LOAD_COD": (("date", p_date),),
    "PAYOUT_DAY": (("date", p_date),),
    "PAYOUT_WEEK": (("week_start_date", p_date),),
    "PRINT_STATEMENT": (
        ("rider_id", p_id("RDR")),
        ("week_start_date", p_date),
    ),
}


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def parse_command_text(
    text: str,
    source: str = "<text>",
) -> list[Command]:
    """Parse command-file text and return validated commands.

    Raises:
        TypeError: If text is not a string.
        CommandParseError: If one or more command lines are invalid.
    """

    if not isinstance(text, str):
        raise TypeError(
            f"text must be a string, got {type(text).__name__}"
        )

    commands: list[Command] = []
    issues: list[CommandIssue] = []

    for line_no, raw in enumerate(text.split("\n"), start=1):
        raw = raw.rstrip("\r")
        stripped = raw.strip()

        if not stripped or stripped.startswith("#"):
            continue

        parts = stripped.split("|")
        name = parts[0].strip()
        args = [arg.strip() for arg in parts[1:]]

        spec = COMMAND_SPECS.get(name)

        if spec is None:
            known = ", ".join(COMMAND_SPECS)
            issues.append(
                CommandIssue(
                    source,
                    line_no,
                    f"unknown command {name!r} (known: {known})",
                )
            )
            continue

        if len(args) != len(spec):
            expected = ", ".join(
                argument_name for argument_name, _ in spec
            )
            issues.append(
                CommandIssue(
                    source,
                    line_no,
                    f"{name} takes {len(spec)} argument(s) "
                    f"({expected}) but {len(args)} given",
                )
            )
            continue

        line_ok = True

        for (arg_name, validator), value in zip(spec, args):
            if value == "":
                issues.append(
                    CommandIssue(
                        source,
                        line_no,
                        f"{name}: argument '{arg_name}' "
                        "is empty but required",
                    )
                )
                line_ok = False
                continue

            try:
                validator(value)
            except ValueError as exc:
                issues.append(
                    CommandIssue(
                        source,
                        line_no,
                        f"{name}: argument '{arg_name}': {exc}",
                    )
                )
                line_ok = False

        if line_ok:
            commands.append(
                Command(
                    name=name,
                    args=tuple(args),
                    line_number=line_no,
                    raw_line=raw,
                )
            )

    if issues:
        raise CommandParseError(issues)

    return commands


def parse_commands(path: str | Path) -> list[Command]:
    """Read a UTF-8 command file and return its validated commands.

    Empty and comments-only files are valid.

    Raises:
        CommandParseError: If the file is missing, invalid UTF-8,
            or contains invalid commands.
    """

    p = Path(path)

    try:
        with open(p, encoding="utf-8-sig", newline=None) as fh:
            text = fh.read()

    except FileNotFoundError:
        raise CommandParseError(
            [CommandIssue(p.name, None, "file not found")]
        ) from None

    except UnicodeDecodeError as exc:
        raise CommandParseError(
            [
                CommandIssue(
                    p.name,
                    None,
                    f"not valid UTF-8: {exc.reason}",
                )
            ]
        ) from None

    return parse_command_text(text, source=p.name)
