import re
from collections import Counter
from pathlib import Path

import pytest

from ps035.command_parser import (
    COMMAND_SPECS, Command, CommandParseError, parse_command_text, parse_commands,
)

PROJECT = Path(__file__).resolve().parent.parent
# The official file may sit at the project root or in data/ (never edited here).
OFFICIAL = next((p for p in (PROJECT / "commands.txt", PROJECT / "data" / "commands.txt") if p.is_file()), None)
README = PROJECT / "data" / "SAMPLE_DATA_README.md"


def write(tmp_path, text, name="cmds.txt"):
    p = tmp_path / name
    p.write_bytes(text.encode("utf-8"))
    return p


def issues_of(text):
    with pytest.raises(CommandParseError) as exc:
        parse_command_text(text, "t.txt")
    return [str(i) for i in exc.value.issues]


# ---- every valid command type -------------------------------------------

VALID_LINES = [
    "PLAN|2026-10-12",
    "PRINT_ROUTE|RDR-01|2026-10-12",
    "LOAD_EVENTS|2026-10-12",
    "LOAD_ODOMETER|2026-10-12",
    "LOAD_COD|2026-10-12",
    "PAYOUT_DAY|2026-10-12",
    "PAYOUT_WEEK|2026-10-12",
    "PRINT_STATEMENT|RDR-08|2026-10-12",
]


def test_valid_lines_cover_every_known_command():
    assert {l.split("|")[0] for l in VALID_LINES} == set(COMMAND_SPECS)


@pytest.mark.parametrize("line", VALID_LINES)
def test_each_valid_command_parses(line):
    (cmd,) = parse_command_text(line + "\n", "t.txt")
    assert isinstance(cmd, Command)
    assert cmd.name == line.split("|")[0]
    assert cmd.args == tuple(line.split("|")[1:])
    assert cmd.line_number == 1 and cmd.raw_line == line


# ---- argument counts / unknown / empty ------------------------------------

@pytest.mark.parametrize("line,expected", [
    ("PLAN", "takes 1 argument(s)"),
    ("PLAN|2026-10-12|extra", "takes 1 argument(s)"),
    ("PRINT_ROUTE|RDR-01", "takes 2 argument(s)"),
    ("PRINT_ROUTE|RDR-01|2026-10-12|x", "takes 2 argument(s)"),
    ("PAYOUT_WEEK", "takes 1 argument(s)"),
    ("PRINT_STATEMENT|RDR-01", "takes 2 argument(s)"),
    ("PLAN|2026-10-12|", "takes 1 argument(s)"),  # trailing pipe = extra (empty) arg
])
def test_wrong_argument_count(line, expected):
    assert any("t.txt line 1" in i and expected in i for i in issues_of(line))


@pytest.mark.parametrize("line", ["plan|2026-10-12", "DELIVER|2026-10-12", "|2026-10-12", "PLAN2|2026-10-12"])
def test_unknown_command_name(line):
    assert any("unknown command" in i for i in issues_of(line))


@pytest.mark.parametrize("line,arg", [
    ("PLAN|", "date"),
    ("PLAN|   ", "date"),
    ("PRINT_ROUTE||2026-10-12", "rider_id"),
    ("PRINT_ROUTE|RDR-01|", "date"),
    ("PRINT_STATEMENT||", "rider_id"),
])
def test_empty_required_argument(line, arg):
    assert any(f"argument '{arg}' is empty" in i for i in issues_of(line))


# ---- dates and ids ---------------------------------------------------------

def test_valid_date_edge_cases_accepted():
    assert len(parse_command_text("PLAN|2028-02-29\nPLAN|2026-12-31\n")) == 2  # leap day


@pytest.mark.parametrize("bad", ["2026-02-30", "2026-13-01", "12-10-2026", "2026/10/12", "2026-1-5", "tomorrow", "2026-10-12 09:00:00"])
def test_invalid_date(bad):
    assert any("argument 'date'" in i for i in issues_of(f"PLAN|{bad}"))


def test_week_start_date_is_validated_as_date():
    assert any("week_start_date" in i for i in issues_of("PAYOUT_WEEK|nope"))


@pytest.mark.parametrize("bad", ["RDR1", "rdr-01", "RDR-", "RDR-0x", "PCL-00001"])
def test_invalid_rider_id(bad):
    assert any("argument 'rider_id'" in i for i in issues_of(f"PRINT_ROUTE|{bad}|2026-10-12"))


# ---- whitespace, blanks, comments, line numbers ---------------------------

def test_blank_and_comment_lines_ignored_with_physical_line_numbers():
    text = "# header\n\nPLAN|2026-10-12\n   \n  # indented comment\nLOAD_EVENTS|2026-10-12\n"
    cmds = parse_command_text(text)
    assert [(c.name, c.line_number) for c in cmds] == [("PLAN", 3), ("LOAD_EVENTS", 6)]


def test_whitespace_around_line_name_and_args_is_trimmed():
    (cmd,) = parse_command_text("  \t PRINT_ROUTE  |  RDR-02 | 2026-10-13  \t\n")
    assert cmd.name == "PRINT_ROUTE" and cmd.args == ("RDR-02", "2026-10-13")
    assert cmd.raw_line.startswith("  \t PRINT_ROUTE")  # original text preserved


def test_internal_spaces_are_not_stripped_so_they_fail_validation():
    assert any("argument 'date'" in i for i in issues_of("PLAN|2026-10- 12"))
    assert any("argument 'rider_id'" in i for i in issues_of("PRINT_ROUTE|RDR -01|2026-10-12"))


def test_no_trailing_newline_and_windows_line_endings(tmp_path):
    p = write(tmp_path, "PLAN|2026-10-12\r\nPAYOUT_DAY|2026-10-12")  # CRLF, no final newline
    assert [(c.name, c.line_number) for c in parse_commands(p)] == [("PLAN", 1), ("PAYOUT_DAY", 2)]


def test_utf8_bom_is_tolerated(tmp_path):
    p = tmp_path / "bom.txt"
    p.write_bytes(b"\xef\xbb\xbf# comment\nPLAN|2026-10-12\n")
    assert [c.line_number for c in parse_commands(p)] == [2]


def test_original_order_kept_and_duplicates_not_removed(tmp_path):
    p = write(tmp_path, "PAYOUT_DAY|2026-10-14\nPLAN|2026-10-12\nPLAN|2026-10-12\nLOAD_COD|2026-10-01\n")
    assert [(c.name, c.args[0]) for c in parse_commands(p)] == [
        ("PAYOUT_DAY", "2026-10-14"), ("PLAN", "2026-10-12"), ("PLAN", "2026-10-12"), ("LOAD_COD", "2026-10-01")]


# ---- empty / comments-only / missing / bad encoding -----------------------

@pytest.mark.parametrize("text", ["", "\n", "\n\n   \n", "# only\n# comments\n", "# no newline at end"])
def test_empty_and_comment_only_files_give_no_commands(tmp_path, text):
    assert parse_commands(write(tmp_path, text)) == []


def test_missing_file_reports_clear_error(tmp_path):
    with pytest.raises(CommandParseError) as exc:
        parse_commands(tmp_path / "nope.txt")
    assert "nope.txt: file not found" in str(exc.value.issues[0])


def test_non_utf8_file_reports_clear_error(tmp_path):
    p = tmp_path / "bad.txt"
    p.write_bytes(b"PLAN|2026-10-12\n\xff\xfe\x00bad\n")
    with pytest.raises(CommandParseError) as exc:
        parse_commands(p)
    assert "not valid UTF-8" in str(exc.value.issues[0])


# ---- many errors at once ---------------------------------------------------

def test_multiple_errors_reported_with_filename_and_line_numbers(tmp_path):
    p = write(tmp_path, "# c\nPLAN|2026-10-12\nPLAN|bad\n\nFLY|x\nPRINT_ROUTE|RDR-01\nPAYOUT_DAY|2026-10-12\nLOAD_COD|\n", "multi.txt")
    with pytest.raises(CommandParseError) as exc:
        parse_commands(p)
    got = {i.line: str(i) for i in exc.value.issues}
    assert sorted(got) == [3, 5, 6, 8]
    assert all(s.startswith(f"multi.txt line {n}:") for n, s in got.items())
    assert "unknown command" in got[5] and "takes 2" in got[6] and "empty" in got[8]


def test_one_line_with_two_bad_arguments_reports_both():
    assert len(issues_of("PRINT_ROUTE|bad|also-bad")) == 2


# ---- official commands.txt --------------------------------------------------

pytestmark_official = pytest.mark.skipif(OFFICIAL is None, reason="official commands.txt not present")


def _independent_count(path):
    """Count command lines without using the parser."""
    return sum(1 for l in path.read_text(encoding="utf-8").splitlines() if l.strip() and not l.strip().startswith("#"))


@pytestmark_official
def test_official_commands_file_parses_and_count_matches_file_contents():
    cmds = parse_commands(OFFICIAL)
    assert len(cmds) == _independent_count(OFFICIAL) > 0
    assert [c.line_number for c in cmds] == sorted(c.line_number for c in cmds)  # file order
    assert {c.name for c in cmds} <= set(COMMAND_SPECS)


@pytestmark_official
def test_official_commands_per_command_counts():
    cmds = parse_commands(OFFICIAL)
    counts = Counter(c.name for c in cmds)
    # structure of the official week: 6 operating days, 8 riders
    assert counts["PLAN"] == counts["LOAD_EVENTS"] == counts["LOAD_ODOMETER"] == counts["PAYOUT_DAY"] == counts["LOAD_COD"] == 6
    assert counts["PRINT_ROUTE"] == 48 and counts["PAYOUT_WEEK"] == 1 and counts["PRINT_STATEMENT"] == 8
    assert cmds[0].name == "PLAN" and cmds[-1].name == "PRINT_STATEMENT"


@pytestmark_official
@pytest.mark.skipif(not README.is_file(), reason="sample README not present")
def test_official_count_matches_number_stated_in_sample_readme():
    m = re.search(r"(\d+) command lines \(excluding comments\)", README.read_text(encoding="utf-8"))
    assert m, "README no longer states a command count"
    assert len(parse_commands(OFFICIAL)) == int(m.group(1))


@pytestmark_official
def test_parsing_does_not_modify_official_file():
    before = OFFICIAL.read_bytes()
    parse_commands(OFFICIAL)
    assert OFFICIAL.read_bytes() == before
