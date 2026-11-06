"""Read-only CSV loading and validation for the seven PS-035 input files.

Beginner notes
--------------
* Every file is described once, in a "spec": a list of columns, each with a
  small *parser* function that turns the text in the CSV into a real Python
  value (a date, a Decimal, ...) or raises ValueError if the text is bad.
* We never stop at the first problem. All problems are collected as
  ``Issue`` objects (file, data-row number, message) and raised together in
  one ``DataValidationError``, so you can fix everything in one pass.
* Row numbers are 1-based DATA rows (header = row 0), like the README.
* Input files are only opened for reading. Nothing is ever written back.
* Money, weights and distances use ``Decimal`` so threshold checks such as
  "exactly 25.0 kg" or "exactly 10% above" are exact, not float-fuzzy.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    file: str
    row: int | None  # 1-based data row; None = whole-file problem (e.g. header)
    message: str

    def __str__(self) -> str:
        where = f"{self.file} row {self.row}" if self.row else self.file
        return f"{where}: {self.message}"


class DataValidationError(Exception):
    def __init__(self, issues: list[Issue]):
        self.issues = issues
        shown = "\n".join(f"  - {i}" for i in issues[:25])
        more = f"\n  ... and {len(issues) - 25} more" if len(issues) > 25 else ""
        super().__init__(f"{len(issues)} data validation issue(s):\n{shown}{more}")


# --------------------------------------------------------------------------
# Small parsers: text -> value, or raise ValueError
# --------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def p_date(s: str) -> date:
    if not _DATE_RE.match(s):
        raise ValueError(f"bad date {s!r}, expected YYYY-MM-DD")
    return datetime.strptime(s, "%Y-%m-%d").date()


def p_time(s: str) -> time:
    if not _TIME_RE.match(s):
        raise ValueError(f"bad time {s!r}, expected HH:MM:SS")
    return datetime.strptime(s, "%H:%M:%S").time()


def p_timestamp(s: str) -> datetime:
    if not _TS_RE.match(s):
        raise ValueError(f"bad timestamp {s!r}, expected YYYY-MM-DD HH:MM:SS")
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def p_decimal(s: str) -> Decimal:
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"not a number: {s!r}") from None
    if not d.is_finite():
        raise ValueError(f"not a finite number: {s!r}")
    return d


def p_nonneg_decimal(s: str) -> Decimal:
    d = p_decimal(s)
    if d < 0:
        raise ValueError(f"must not be negative: {s!r}")
    return d


def p_pos_int(s: str) -> int:
    if not re.fullmatch(r"\d+", s) or int(s) <= 0:
        raise ValueError(f"must be a positive whole number: {s!r}")
    return int(s)


def p_bool(s: str) -> bool:
    if s not in ("true", "false"):
        raise ValueError(f"must be 'true' or 'false', got {s!r}")
    return s == "true"


def p_id(prefix: str) -> Callable[[str], str]:
    pattern = re.compile(rf"^{prefix}-\d+$")

    def parse(s: str) -> str:
        if not pattern.match(s):
            raise ValueError(f"bad ID {s!r}, expected {prefix}-<digits>")
        return s

    return parse


def p_enum(*allowed: str) -> Callable[[str], str]:
    def parse(s: str) -> str:
        if s not in allowed:
            raise ValueError(f"{s!r} not one of {', '.join(allowed)}")
        return s

    return parse


def p_text(s: str) -> str:
    if not s.strip():
        raise ValueError("must not be empty")
    return s


def p_text_or_empty(s: str) -> str:
    return s


def p_range(lo: str, hi: str) -> Callable[[str], Decimal]:
    lo_d, hi_d = Decimal(lo), Decimal(hi)

    def parse(s: str) -> Decimal:
        d = p_decimal(s)
        if not lo_d <= d <= hi_d:
            raise ValueError(f"{s!r} outside {lo}..{hi}")
        return d

    return parse


# --------------------------------------------------------------------------
# Record types (one per file)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Hub:
    hub_id: str
    hub_name: str
    lat: Decimal
    lon: Decimal


@dataclass(frozen=True)
class Rider:
    rider_id: str
    rider_name: str
    vehicle_type: str
    capacity_parcels: int
    capacity_kg: Decimal
    home_hub: str
    default_shift_start: time
    default_shift_end: time


@dataclass(frozen=True)
class RiderShift:
    rider_id: str
    shift_date: date
    shift_start: time
    shift_end: time


@dataclass(frozen=True)
class Parcel:
    parcel_id: str
    pool_date: date
    dest_lat: Decimal
    dest_lon: Decimal
    weight_kg: Decimal
    window_start: time
    window_end: time
    priority: str
    cod_flag: bool
    cod_amount: Decimal


@dataclass(frozen=True)
class DeliveryEvent:
    event_id: str
    event_date: date
    parcel_id: str
    rider_id: str
    event_ts: datetime
    event_type: str
    reason_code: str  # "" when not applicable
    attempt_no: int
    cod_collected: Decimal


@dataclass(frozen=True)
class OdometerReading:
    rider_id: str
    odo_date: date
    start_km: Decimal
    end_km: Decimal


@dataclass(frozen=True)
class CodDeposit:
    deposit_id: str
    rider_id: str
    deposit_date: date
    deposit_ts: datetime
    amount: Decimal


# --------------------------------------------------------------------------
# File specs: (file name, record class, [(column, parser), ...])
# Column ORDER here is the documented order; headers must match exactly.
# --------------------------------------------------------------------------

_VEHICLES = p_enum("BIKE", "EV_BIKE", "VAN")

SPECS: dict[str, tuple[type, list[tuple[str, Callable[[str], Any]]]]] = {
    "hub.csv": (Hub, [
        ("hub_id", p_id("HUB")), ("hub_name", p_text),
        ("lat", p_range("-90", "90")), ("lon", p_range("-180", "180")),
    ]),
    "riders.csv": (Rider, [
        ("rider_id", p_id("RDR")), ("rider_name", p_text),
        ("vehicle_type", _VEHICLES), ("capacity_parcels", p_pos_int),
        ("capacity_kg", p_nonneg_decimal), ("home_hub", p_id("HUB")),
        ("default_shift_start", p_time), ("default_shift_end", p_time),
    ]),
    "rider_shifts.csv": (RiderShift, [
        ("rider_id", p_id("RDR")), ("shift_date", p_date),
        ("shift_start", p_time), ("shift_end", p_time),
    ]),
    "parcels.csv": (Parcel, [
        ("parcel_id", p_id("PCL")), ("pool_date", p_date),
        ("dest_lat", p_range("-90", "90")), ("dest_lon", p_range("-180", "180")),
        ("weight_kg", p_nonneg_decimal), ("window_start", p_time),
        ("window_end", p_time), ("priority", p_enum("EXPRESS", "STANDARD")),
        ("cod_flag", p_bool), ("cod_amount", p_nonneg_decimal),
    ]),
    "delivery_events.csv": (DeliveryEvent, [
        ("event_id", p_id("EVT")), ("event_date", p_date),
        ("parcel_id", p_id("PCL")), ("rider_id", p_id("RDR")),
        ("event_ts", p_timestamp),
        ("event_type", p_enum("DELIVERED", "FAILED", "RESCHEDULED", "COMPLAINT")),
        ("reason_code", p_text_or_empty),
        ("attempt_no", p_enum("1", "2")),
        ("cod_collected", p_nonneg_decimal),
    ]),
    "odometer.csv": (OdometerReading, [
        ("rider_id", p_id("RDR")), ("odo_date", p_date),
        ("start_km", p_nonneg_decimal), ("end_km", p_nonneg_decimal),
    ]),
    "cod_deposits.csv": (CodDeposit, [
        ("deposit_id", p_id("DEP")), ("rider_id", p_id("RDR")),
        ("deposit_date", p_date), ("deposit_ts", p_timestamp),
        ("amount", p_nonneg_decimal),
    ]),
}


# --------------------------------------------------------------------------
# Generic single-file reader
# --------------------------------------------------------------------------


def _read_file(data_dir: Path, fname: str, issues: list[Issue]) -> list[tuple[int, Any]]:
    """Return [(row_number, record), ...] for rows that parsed cleanly.

    Header / parse problems are appended to ``issues`` (never raised here).
    """
    cls, columns = SPECS[fname]
    path = data_dir / fname
    if not path.is_file():
        issues.append(Issue(fname, None, "file not found"))
        return []
    names = [c for c, _ in columns]
    with open(path, newline="", encoding="utf-8") as fh:  # read-only
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            issues.append(Issue(fname, None, "file is empty (no header)"))
            return []
        missing = [n for n in names if n not in header]
        extra = [h for h in header if h not in names]
        if missing:
            issues.append(Issue(fname, None, f"missing required column(s): {', '.join(missing)}"))
        if extra:
            issues.append(Issue(fname, None, f"unexpected column(s): {', '.join(extra)}"))
        if missing:
            return []
        if header != names:
            issues.append(Issue(fname, None, f"columns present but not in documented order: {header}"))
        index = {h: i for i, h in enumerate(header)}
        out: list[tuple[int, Any]] = []
        for rownum, raw in enumerate(reader, start=1):
            if len(raw) != len(header):
                issues.append(Issue(fname, rownum, f"expected {len(header)} fields, found {len(raw)}"))
                continue
            values, ok = {}, True
            for col, parser in columns:
                try:
                    values[col] = parser(raw[index[col]])
                except ValueError as exc:
                    issues.append(Issue(fname, rownum, f"column '{col}': {exc}"))
                    ok = False
            if ok:
                if cls is DeliveryEvent:
                    values["attempt_no"] = int(values["attempt_no"])
                out.append((rownum, cls(**values)))
        return out


# --------------------------------------------------------------------------
# Cross-record checks (uniqueness, foreign keys, sensible relationships)
# --------------------------------------------------------------------------


def _check_unique(fname: str, rows: list[tuple[int, Any]], key: Callable[[Any], Any],
                  label: str, issues: list[Issue]) -> None:
    seen: dict[Any, int] = {}
    for n, rec in rows:
        k = key(rec)
        if k in seen:
            issues.append(Issue(fname, n, f"duplicate {label} {k!r} (first seen at row {seen[k]})"))
        else:
            seen[k] = n


@dataclass
class DataSet:
    hubs: list[Hub] = field(default_factory=list)
    riders: list[Rider] = field(default_factory=list)
    shifts: list[RiderShift] = field(default_factory=list)
    parcels: list[Parcel] = field(default_factory=list)
    events: list[DeliveryEvent] = field(default_factory=list)
    odometer: list[OdometerReading] = field(default_factory=list)
    cod_deposits: list[CodDeposit] = field(default_factory=list)


def load_dataset(data_dir: str | Path) -> DataSet:
    """Load and validate all seven CSVs from ``data_dir``.

    Raises DataValidationError listing every problem found. Never modifies files.
    """
    d = Path(data_dir)
    issues: list[Issue] = []
    r = {f: _read_file(d, f, issues) for f in SPECS}

    hubs, riders, shifts = r["hub.csv"], r["riders.csv"], r["rider_shifts.csv"]
    parcels, events = r["parcels.csv"], r["delivery_events.csv"]
    odo, deps = r["odometer.csv"], r["cod_deposits.csv"]

    # Primary keys
    _check_unique("hub.csv", hubs, lambda x: x.hub_id, "hub_id", issues)
    _check_unique("riders.csv", riders, lambda x: x.rider_id, "rider_id", issues)
    _check_unique("parcels.csv", parcels, lambda x: x.parcel_id, "parcel_id", issues)
    _check_unique("delivery_events.csv", events, lambda x: x.event_id, "event_id", issues)
    _check_unique("cod_deposits.csv", deps, lambda x: x.deposit_id, "deposit_id", issues)
    # Composite keys: one shift / one odometer reading per rider per day
    _check_unique("rider_shifts.csv", shifts, lambda x: (x.rider_id, x.shift_date), "(rider_id, shift_date)", issues)
    _check_unique("odometer.csv", odo, lambda x: (x.rider_id, x.odo_date), "(rider_id, odo_date)", issues)

    hub_ids = {x.hub_id for _, x in hubs}
    rider_ids = {x.rider_id for _, x in riders}
    parcel_ids = {x.parcel_id for _, x in parcels}

    def fk(fname, rows, attr, valid, target):
        for n, rec in rows:
            v = getattr(rec, attr)
            if v not in valid:
                issues.append(Issue(fname, n, f"{attr} {v!r} not found in {target}"))

    fk("riders.csv", riders, "home_hub", hub_ids, "hub.csv")
    fk("rider_shifts.csv", shifts, "rider_id", rider_ids, "riders.csv")
    fk("delivery_events.csv", events, "rider_id", rider_ids, "riders.csv")
    fk("delivery_events.csv", events, "parcel_id", parcel_ids, "parcels.csv")
    fk("odometer.csv", odo, "rider_id", rider_ids, "riders.csv")
    fk("cod_deposits.csv", deps, "rider_id", rider_ids, "riders.csv")

    # Row-level relationships
    for n, s in shifts:
        if s.shift_end <= s.shift_start:
            issues.append(Issue("rider_shifts.csv", n, "shift_end must be after shift_start"))
    for n, rd in riders:
        if rd.default_shift_end <= rd.default_shift_start:
            issues.append(Issue("riders.csv", n, "default_shift_end must be after default_shift_start"))
    for n, p in parcels:
        if p.window_end < p.window_start:
            issues.append(Issue("parcels.csv", n, "window_end is before window_start"))
        if not p.cod_flag and p.cod_amount != 0:
            issues.append(Issue("parcels.csv", n, "cod_amount must be 0.00 when cod_flag is false"))
    for n, e in events:
        if e.event_ts.date() != e.event_date:
            issues.append(Issue("delivery_events.csv", n, "event_ts date differs from event_date"))
        if e.event_type == "FAILED" and not e.reason_code:
            issues.append(Issue("delivery_events.csv", n, "FAILED event needs a reason_code"))
        if e.event_type in ("DELIVERED", "RESCHEDULED") and e.reason_code:
            issues.append(Issue("delivery_events.csv", n, f"{e.event_type} event must have an empty reason_code"))
    for n, o in odo:
        if o.end_km < o.start_km:
            issues.append(Issue("odometer.csv", n, "end_km is less than start_km"))
    for n, dep in deps:
        if dep.deposit_ts.date() != dep.deposit_date:
            issues.append(Issue("cod_deposits.csv", n, "deposit_ts date differs from deposit_date"))
    # NOTE: events outside a rider's shift window are deliberately NOT errors:
    # the event log is authoritative for pay.

    if issues:
        raise DataValidationError(issues)

    return DataSet(
        hubs=[x for _, x in hubs], riders=[x for _, x in riders],
        shifts=[x for _, x in shifts], parcels=[x for _, x in parcels],
        events=[x for _, x in events], odometer=[x for _, x in odo],
        cod_deposits=[x for _, x in deps],
    )
