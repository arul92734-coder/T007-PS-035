"""Parse the official PS-035 ``commands.txt`` into typed command records.

Beginner notes
--------------
* This module only READS and CHECKS a command file. It never runs a command
  and never writes any file. Running commands is a later module's job.
* Format (from the official README): one command per line,
  ``COMMAND_NAME|arg1|arg2|...``. Blank lines and lines starting with ``#``
  are ignored. Commands keep their original file order.
* Like ``loader.py``, we do not stop at the first problem: every bad line is
  collected as a ``CommandIssue`` (file + physical line number + explanation)
  and raised together in one ``CommandParseError``.
* Physical line numbers count every line of the file, including comments and
  blank lines, so they match what an editor shows.

Choices where the README is silent (documented, deliberately lenient):
* Spaces/tabs around the whole line, the command name and each argument are
  trimmed. Spaces INSIDE an argument are kept (and then fail validation for
  dates and IDs, so they are never silently "fixed").
* Command names are case-sensitive (``PLAN``, not ``plan``).
* Every argument of every known command is required, so an empty argument
  (``PLAN|``) is an error, even though the README says an empty argument
  generally means "not supplied": none of these commands allows that.
* Dates must be real calendar dates in ``YYYY-MM-DD``. No date-range or
  Monday-only rule is applied because the README does not specify one.
* Ids must look like ``RDR-<digits>`` (same rule the data loader uses).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ps035.loader import p_date, p_id  # reuse the loader's rules; no second format

# --------------------------------------------------------------------------
# Public records
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Command:
    """One parsed command line."""

    name: str                 # e.g. "PRINT_ROUTE"
    args: tuple[str, ...]     # validated argument text, in README order
    line_number: int          # 1-based physical line in the file
    raw_line: str             # the original line (without the newline)


@dataclass(frozen=True)
class CommandIssue:
    """One problem found in a command file."""

    file: str
    line: int | None          # None = whole-file problem
    message: str

    def __str__(self) -> str:
        where = f"{self.file} line {self.line}" if self.line else self.file
        return f"{where}: {self.message}"


class CommandParseError(Exception):
    """Raised with ALL problems found in a command file."""

    def __init__(self, issues: list[CommandIssue]):
        self.issues = issues
        shown = "\n".join(f"  - {i}" for i in issues[:25])
        more = f"\n  ... and {len(issues) - 25} more" if len(issues) > 25 else ""
        super().__init__(f"{len(issues)} command file issue(s):\n{shown}{more}")


# --------------------------------------------------------------------------
# What the README says each command takes: (argument name, validator)
# --------------------------------------------------------------------------

_Validator = Callable[[str], object]

COMMAND_SPECS: dict[str, tuple[tuple[str, _Validator], ...]] = {
    "PLAN": (("date", p_date),),
    "PRINT_ROUTE": (("rider_id", p_id("RDR")), ("date", p_date)),
    "LOAD_EVENTS": (("date", p_date),),
    "LOAD_ODOMETER": (("date", p_date),),
    "LOAD_COD": (("date", p_date),),
    "PAYOUT_DAY": (("date", p_date),),
    "PAYOUT_WEEK": (("week_start_date", p_date),),
    "PRINT_STATEMENT": (("rider_id", p_id("RDR")), ("week_start_date", p_date)),
}


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_command_text(text: str, source: str = "<text>") -> list[Command]:
    """Parse command-file text. ``source`` is only used in error messages.

    Raises CommandParseError listing every bad line.
    """
    commands: list[Command] = []
    issues: list[CommandIssue] = []

    # text is expected to have "\n" line endings (parse_commands normalises
    # Windows "\r\n"); split on "\n" only so line numbers stay physical.
    for line_no, raw in enumerate(text.split("\n"), start=1):
        raw = raw.rstrip("\r")
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue  # blank or comment

        parts = stripped.split("|")
        name = parts[0].strip()
        args = [a.strip() for a in parts[1:]]

        spec = COMMAND_SPECS.get(name)
        if spec is None:
            known = ", ".join(COMMAND_SPECS)
            issues.append(CommandIssue(source, line_no, f"unknown command {name!r} (known: {known})"))
            continue

        if len(args) != len(spec):
            expected = ", ".join(n for n, _ in spec)
            issues.append(CommandIssue(
                source, line_no,
                f"{name} takes {len(spec)} argument(s) ({expected}) but {len(args)} given"))
            continue

        line_ok = True
        for (arg_name, validator), value in zip(spec, args):
            if value == "":
                issues.append(CommandIssue(source, line_no, f"{name}: argument '{arg_name}' is empty but required"))
                line_ok = False
                continue
            try:
                validator(value)
            except ValueError as exc:
                issues.append(CommandIssue(source, line_no, f"{name}: argument '{arg_name}': {exc}"))
                line_ok = False
        if line_ok:
            commands.append(Command(name=name, args=tuple(args), line_number=line_no, raw_line=raw))

    if issues:
        raise CommandParseError(issues)
    return commands


def parse_commands(path: str | Path) -> list[Command]:
    """Read ``path`` (UTF-8, read-only) and return its commands in file order.

    An empty or comments-only file is valid and returns an empty list.
    Raises CommandParseError if the file is missing, not valid UTF-8, or has
    any bad command line.
    """
    p = Path(path)
    try:
        # utf-8-sig: tolerate the invisible BOM that some Windows editors add.
        # newline=None: turn Windows "\r\n" into "\n" while reading.
        with open(p, encoding="utf-8-sig", newline=None) as fh:
            text = fh.read()
    except FileNotFoundError:
        raise CommandParseError([CommandIssue(p.name, None, "file not found")]) from None
    except UnicodeDecodeError as exc:
        raise CommandParseError([CommandIssue(p.name, None, f"not valid UTF-8: {exc.reason}")]) from None
    return parse_command_text(text, source=p.name)
