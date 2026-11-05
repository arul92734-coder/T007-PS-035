"""Delivery event recording and parcel lifecycle (BR-11) for PS-035.

Beginner notes
--------------
* This module RECORDS and VALIDATES delivery events and derives each parcel's
  lifecycle (reschedule count, priority, next eligible date, RTO). It does NOT
  calculate pay, penalties, COD deposits or shortfalls. Later payout modules
  read events from here and apply their own rules.
* It reuses the loader's records and error style: events are the loader's
  frozen ``DeliveryEvent``; problems are ``Issue`` objects raised together in
  one ``DataValidationError``. Nothing is ever written to a CSV, and loaded
  ``Parcel`` / ``DeliveryEvent`` objects are never modified. Lifecycle state is
  DERIVED from the event history each time, so history can never be corrupted.
* Money is ``Decimal``. Timestamps are timezone-naive ``YYYY-MM-DD HH:MM:SS``.

Event types
-----------
``DELIVERED``, ``FAILED``, ``RESCHEDULED`` drive the parcel lifecycle.
``COMPLAINT`` is a fourth type that exists in the official schema and sample
(3 rows). It is kept in the history but never changes parcel status. Its
``reason_code`` is free text (README).

Lifecycle rules (BR-11 plus documented assumptions)
---------------------------------------------------
* Attempt number = (reschedules before this event) + 1. The sample log follows
  this on every row.
* One outcome per attempt: after DELIVERED / FAILED on an attempt, a second
  DELIVERED / FAILED for the same attempt is a duplicate. A FAILED attempt may
  be followed by RESCHEDULED (design state diagram). A FAILED parcel that is
  not rescheduled is NOT re-pooled (the sample never re-attempts one).
* RESCHEDULED: count + 1, priority upgraded one level (STANDARD -> EXPRESS;
  EXPRESS stays EXPRESS, the top of the ladder), next eligible date = event
  date + 1 calendar day. The 2nd reschedule makes the parcel RTO at once.
* Later events on a rescheduled parcel must have ``event_date >= next
  eligible date``. So a parcel cannot be rescheduled twice on the same day.
* After DELIVERED or RTO, no further DELIVERED / FAILED / RESCHEDULED events
  are accepted (COMPLAINT is still allowed after DELIVERED, as in the sample,
  but not after RTO).
* Events may be recorded out of order; the whole parcel history is re-checked
  in (event_ts, event_id) order, and the new event is refused if that
  history becomes contradictory.

Failure reasons: new events must use NOT_HOME, WRONG_ADDRESS or RIDER_FAULT.
Loading EXISTING history is lenient: other text (the sample's CUSTOMER_BUSY) is
accepted and flagged via ``EventLog.flagged_events()``. No pay is decided here.

Shift windows and route ETAs are deliberately NOT checked: the event log is
authoritative for payout.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

from ps035.loader import (
    DataSet,
    DataValidationError,
    DeliveryEvent,
    Issue,
    Parcel,
    Rider,
    load_dataset,
    p_date,
    p_id,
    p_timestamp,
)

# --------------------------------------------------------------------------
# Constants (strings, like the loader's priority / event_type values)
# --------------------------------------------------------------------------

DELIVERED = "DELIVERED"
FAILED = "FAILED"
RESCHEDULED = "RESCHEDULED"
COMPLAINT = "COMPLAINT"

LIFECYCLE_EVENT_TYPES = (DELIVERED, FAILED, RESCHEDULED)
EVENT_TYPES = (DELIVERED, FAILED, RESCHEDULED, COMPLAINT)  # = loader's set

FAILURE_REASONS = ("NOT_HOME", "WRONG_ADDRESS", "RIDER_FAULT")  # README / BR-05

EXPRESS = "EXPRESS"
STANDARD = "STANDARD"
PRIORITY_UPGRADE = {STANDARD: EXPRESS, EXPRESS: EXPRESS}  # one level up; top stays

RESCHEDULE_LIMIT = 2  # BR-11: rescheduled twice -> RTO

# Parcel status values derived from the event history
STATUS_PENDING = "PENDING"          # no lifecycle event yet
STATUS_DELIVERED = "DELIVERED"
STATUS_FAILED = "FAILED"            # last attempt failed, not rescheduled
STATUS_RESCHEDULED = "RESCHEDULED"  # waiting for the next-day pool
STATUS_RTO = "RTO"                  # left the delivery pool for good

FLAG_UNRECOGNISED_REASON = "UNRECOGNISED_FAILURE_REASON"

_FILE = "delivery_events.csv"  # label used in Issue objects, like the loader


# --------------------------------------------------------------------------
# Lifecycle record
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ParcelLifecycle:
    """Derived state of one parcel. Built from events; never stored on Parcel."""

    parcel_id: str
    original_priority: str
    current_priority: str            # after any BR-11 upgrade
    reschedule_count: int
    status: str                      # one of the STATUS_* values
    next_eligible_date: date | None  # only while status == RESCHEDULED
    attempts_recorded: int           # DELIVERED / FAILED / RESCHEDULED events seen
    last_event_id: str | None        # last lifecycle event (None if none)

    @property
    def is_rto(self) -> bool:
        return self.status == STATUS_RTO

    @property
    def is_delivered(self) -> bool:
        return self.status == STATUS_DELIVERED

    @property
    def priority_upgraded(self) -> bool:
        return self.current_priority != self.original_priority

    @property
    def awaiting_repool(self) -> bool:
        return self.status == STATUS_RESCHEDULED


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _event_sort_key(e: DeliveryEvent) -> tuple[datetime, int, str]:
    m = re.search(r"(\d+)$", e.event_id)
    return (e.event_ts, int(m.group(1)) if m else 0, e.event_id)


def _parse_money(value: object, label: str) -> Decimal:
    """Strict money parser: str / int / Decimal only (floats are refused)."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{label} must be a Decimal, int or numeric string, not {type(value).__name__}")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, (int, str)):
        try:
            d = Decimal(value.strip() if isinstance(value, str) else value)
        except InvalidOperation:
            raise ValueError(f"{label} is not a number: {value!r}") from None
    else:
        raise ValueError(f"{label} must be a Decimal, int or numeric string, not {type(value).__name__}")
    if not d.is_finite():
        raise ValueError(f"{label} is not a finite number: {value!r}")
    if d < 0:
        raise ValueError(f"{label} must not be negative: {value!r}")
    if d != d.quantize(Decimal("0.01")):
        raise ValueError(f"{label} has more than 2 decimal places: {value!r}")
    return d


def _event_field_issues(e: DeliveryEvent, parcel: Parcel, *, strict_reason: bool) -> list[str]:
    """Rules that look at ONE event and its parcel (not at history)."""
    msgs: list[str] = []
    if e.event_type not in EVENT_TYPES:
        return [f"event_type {e.event_type!r} not one of {', '.join(EVENT_TYPES)}"]
    if e.attempt_no not in (1, 2):
        msgs.append(f"attempt_no {e.attempt_no!r} not one of 1, 2")
    if e.event_ts.tzinfo is not None:
        msgs.append("event_ts must be timezone-naive (project uses local IST timestamps)")
    if e.event_ts.date() != e.event_date:
        msgs.append("event_ts date differs from event_date")
    if e.event_date < parcel.pool_date:
        msgs.append(f"event_date {e.event_date} is before the parcel's pool_date {parcel.pool_date}")

    reason = e.reason_code
    cod = e.cod_collected
    if e.event_type == DELIVERED:
        if reason:
            msgs.append("DELIVERED event must have an empty reason_code")
        if cod < 0:
            msgs.append("cod_collected must not be negative")
        elif cod > parcel.cod_amount:
            msgs.append(f"cod_collected {cod} exceeds the parcel's cod_amount {parcel.cod_amount}")
    elif e.event_type == FAILED:
        if not reason:
            msgs.append("FAILED event needs a reason_code")
        elif strict_reason and reason not in FAILURE_REASONS:
            msgs.append(f"reason_code {reason!r} not one of {', '.join(FAILURE_REASONS)}")
        if cod != 0:
            msgs.append("cod_collected must be 0 on a FAILED event")
    elif e.event_type == RESCHEDULED:
        if reason:
            msgs.append("RESCHEDULED event must have an empty reason_code")
        if cod != 0:
            msgs.append("cod_collected must be 0 on a RESCHEDULED event")
    else:  # COMPLAINT
        if not reason.strip():
            msgs.append("COMPLAINT event needs a free-text reason_code")
        if cod != 0:
            msgs.append("cod_collected must be 0 on a COMPLAINT event")
    return msgs


def _walk_history(parcel: Parcel, sorted_events: list[DeliveryEvent],
                  problems: dict[str, list[str]] | None = None) -> ParcelLifecycle:
    """Replay one parcel's events in order, deriving its lifecycle.

    If ``problems`` is given, contradictions are added to it keyed by event_id
    (the event is then skipped, so one bad event does not hide the rest).
    """
    def bad(ev: DeliveryEvent, msg: str) -> None:
        if problems is not None:
            problems.setdefault(ev.event_id, []).append(msg)

    count = 0
    priority = parcel.priority
    status = STATUS_PENDING
    next_date: date | None = None
    attempts = 0
    last_id: str | None = None
    failed_this_attempt = False
    seen_ids: set[str] = set()

    for ev in sorted_events:
        if ev.event_id in seen_ids:
            bad(ev, f"duplicate event_id {ev.event_id!r} on parcel {parcel.parcel_id}")
            continue
        seen_ids.add(ev.event_id)

        if status == STATUS_RTO:
            bad(ev, f"event after RTO: parcel {parcel.parcel_id} left the pool after {RESCHEDULE_LIMIT} reschedules")
            continue
        if ev.event_type == COMPLAINT:
            continue  # recorded in history only; no lifecycle effect
        if status == STATUS_DELIVERED:
            bad(ev, f"{ev.event_type} after DELIVERED: parcel {parcel.parcel_id} is already delivered")
            continue
        if next_date is not None and ev.event_date < next_date:
            bad(ev, f"{ev.event_type} on {ev.event_date} is before the parcel's next eligible date {next_date}")
            continue
        expected = count + 1
        if ev.attempt_no != expected:
            bad(ev, f"attempt_no {ev.attempt_no} should be {expected} ({count} earlier reschedule(s))")
            continue

        if ev.event_type == RESCHEDULED:
            count += 1
            priority = PRIORITY_UPGRADE.get(priority, priority)
            attempts += 1
            last_id = ev.event_id
            failed_this_attempt = False
            if count >= RESCHEDULE_LIMIT:
                status, next_date = STATUS_RTO, None
            else:
                status, next_date = STATUS_RESCHEDULED, ev.event_date + timedelta(days=1)
        else:  # DELIVERED or FAILED: one outcome per attempt
            if failed_this_attempt:
                bad(ev, f"duplicate outcome: attempt {expected} of {parcel.parcel_id} already has a FAILED event")
                continue
            attempts += 1
            last_id = ev.event_id
            next_date = None
            if ev.event_type == DELIVERED:
                status = STATUS_DELIVERED
            else:
                status = STATUS_FAILED
                failed_this_attempt = True

    return ParcelLifecycle(
        parcel_id=parcel.parcel_id, original_priority=parcel.priority,
        current_priority=priority, reschedule_count=count, status=status,
        next_eligible_date=next_date, attempts_recorded=attempts, last_event_id=last_id,
    )


# --------------------------------------------------------------------------
# The event log
# --------------------------------------------------------------------------


class EventLog:
    """Validated, append-only delivery event history for known parcels / riders.

    Build it with ``EventLog.from_dataset(ds)`` or ``load_event_log(data_dir)``.
    Existing history is validated at construction (all problems are raised
    together as ``DataValidationError``). ``record_event`` appends one new
    validated event. Events are never edited or removed.
    """

    def __init__(self, parcels: Iterable[Parcel], riders: Iterable[Rider],
                 events: Iterable[DeliveryEvent] = ()):
        self._parcels: dict[str, Parcel] = {p.parcel_id: p for p in parcels}
        self._rider_ids: frozenset[str] = frozenset(r.rider_id for r in riders)
        self._events: list[DeliveryEvent] = []                  # insertion order
        self._by_parcel: dict[str, list[DeliveryEvent]] = {}    # kept sorted
        self._event_ids: set[str] = set()
        self._load_history(list(events))

    # ---- construction --------------------------------------------------

    @classmethod
    def from_dataset(cls, ds: DataSet) -> "EventLog":
        return cls(ds.parcels, ds.riders, ds.events)

    def _load_history(self, events: list[DeliveryEvent]) -> None:
        issues: list[Issue] = []
        row_of: dict[int, int] = {}
        grouped: dict[str, list[DeliveryEvent]] = {}
        seen: dict[str, int] = {}
        for n, e in enumerate(events, start=1):
            row_of[id(e)] = n
            if not isinstance(e, DeliveryEvent):
                issues.append(Issue(_FILE, n, "not a DeliveryEvent record"))
                continue
            if e.event_id in seen:
                issues.append(Issue(_FILE, n, f"duplicate event_id {e.event_id!r} (first seen at row {seen[e.event_id]})"))
                continue
            seen[e.event_id] = n
            parcel = self._parcels.get(e.parcel_id)
            if parcel is None:
                issues.append(Issue(_FILE, n, f"parcel_id {e.parcel_id!r} not found in parcels.csv"))
            if e.rider_id not in self._rider_ids:
                issues.append(Issue(_FILE, n, f"rider_id {e.rider_id!r} not found in riders.csv"))
            if parcel is None:
                continue
            for msg in _event_field_issues(e, parcel, strict_reason=False):
                issues.append(Issue(_FILE, n, msg))
            grouped.setdefault(e.parcel_id, []).append(e)

        for pid, evs in grouped.items():
            problems: dict[str, list[str]] = {}
            _walk_history(self._parcels[pid], sorted(evs, key=_event_sort_key), problems)
            for e in evs:
                for msg in problems.get(e.event_id, []):
                    issues.append(Issue(_FILE, row_of[id(e)], msg))
        if issues:
            issues.sort(key=lambda i: (i.row or 0, i.message))
            raise DataValidationError(issues)

        for e in events:
            self._events.append(e)
            self._event_ids.add(e.event_id)
            self._by_parcel.setdefault(e.parcel_id, []).append(e)
        for lst in self._by_parcel.values():
            lst.sort(key=_event_sort_key)

    # ---- recording -----------------------------------------------------

    def record_event(self, *, parcel_id: str, rider_id: str,
                     event_ts: datetime | str, event_type: str,
                     reason_code: str = "", cod_collected: object = None,
                     attempt_no: int | None = None, event_id: str | None = None,
                     event_date: date | str | None = None) -> DeliveryEvent:
        """Validate and append one event; return the stored ``DeliveryEvent``.

        ``cod_collected`` may be omitted/None (stored as 0.00). ``attempt_no``
        may be omitted (derived from the history); if given it must match.
        ``event_id`` may be omitted (next free ``EVT-nnnnn`` is used).
        Raises ``DataValidationError`` (row = None) listing every problem; on
        error nothing is recorded.
        """
        msgs: list[str] = []

        # -- parse raw inputs (same formats as the CSV) -------------------
        ts = None
        if isinstance(event_ts, datetime):
            ts = event_ts
            if ts.tzinfo is not None:
                msgs.append("event_ts must be timezone-naive (project uses local IST timestamps)")
        else:
            try:
                ts = p_timestamp(event_ts) if isinstance(event_ts, str) else None
                if ts is None:
                    msgs.append(f"event_ts must be a datetime or 'YYYY-MM-DD HH:MM:SS', got {type(event_ts).__name__}")
            except ValueError as exc:
                msgs.append(f"event_ts: {exc}")

        edate = None
        if event_date is None:
            edate = ts.date() if ts else None
        elif isinstance(event_date, datetime):
            msgs.append("event_date must be a date, not a datetime")
        elif isinstance(event_date, date):
            edate = event_date
        else:
            try:
                edate = p_date(event_date)  # type: ignore[arg-type]
            except (ValueError, TypeError) as exc:
                msgs.append(f"event_date: {exc}")

        if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
            msgs.append(f"event_type {event_type!r} not one of {', '.join(EVENT_TYPES)}")

        cod = Decimal("0.00")
        if cod_collected is not None and cod_collected != "":
            try:
                cod = _parse_money(cod_collected, "cod_collected")
            except ValueError as exc:
                msgs.append(str(exc))

        if attempt_no is not None and (isinstance(attempt_no, bool) or attempt_no not in (1, 2)):
            msgs.append(f"attempt_no {attempt_no!r} not one of 1, 2")
        if not isinstance(reason_code, str):
            msgs.append("reason_code must be text")
            reason_code = ""

        parcel = self._parcels.get(parcel_id) if isinstance(parcel_id, str) else None
        if parcel is None:
            msgs.append(f"parcel_id {parcel_id!r} not found in parcels.csv")
        if not isinstance(rider_id, str) or rider_id not in self._rider_ids:
            msgs.append(f"rider_id {rider_id!r} not found in riders.csv")

        if event_id is None:
            event_id = self._next_event_id()
        else:
            try:
                p_id("EVT")(event_id)
            except (ValueError, TypeError):
                msgs.append(f"bad event_id {event_id!r}, expected EVT-<digits>")
            else:
                if event_id in self._event_ids:
                    msgs.append(f"duplicate event_id {event_id!r}: event ids are never reused or overwritten")

        if msgs or parcel is None or ts is None or edate is None:
            raise DataValidationError([Issue(_FILE, None, m) for m in msgs])

        # -- derive attempt number, then check this event + its history ----
        history = self._by_parcel.get(parcel_id, [])
        probe = self._placeholder(event_id, edate, ts, parcel_id, rider_id, event_type, reason_code, cod)
        probe_key = _event_sort_key(probe)
        derived = 1 + sum(1 for h in history
                          if h.event_type == RESCHEDULED and _event_sort_key(h) < probe_key)
        if attempt_no is None:
            attempt_no = derived if derived <= 2 else 2  # a 3rd attempt is rejected by the lifecycle check
        new = DeliveryEvent(event_id=event_id, event_date=edate, parcel_id=parcel_id,
                            rider_id=rider_id, event_ts=ts, event_type=event_type,
                            reason_code=reason_code, attempt_no=attempt_no, cod_collected=cod)

        msgs = _event_field_issues(new, parcel, strict_reason=True)
        candidate = sorted(history + [new], key=_event_sort_key)
        problems: dict[str, list[str]] = {}
        _walk_history(parcel, candidate, problems)
        msgs += problems.get(event_id, [])
        # recording must not make EARLIER history contradictory (e.g. back-dating)
        for other in candidate:
            if other.event_id != event_id and other.event_id in problems:
                msgs.append(f"would make existing event {other.event_id} invalid: "
                            + "; ".join(problems[other.event_id]))
        if msgs:
            raise DataValidationError([Issue(_FILE, None, m) for m in msgs])

        # -- commit (append only) -----------------------------------------
        self._events.append(new)
        self._event_ids.add(event_id)
        self._by_parcel.setdefault(parcel_id, []).append(new)
        self._by_parcel[parcel_id].sort(key=_event_sort_key)
        return new

    @staticmethod
    def _placeholder(event_id, edate, ts, parcel_id, rider_id, event_type, reason, cod) -> DeliveryEvent:
        return DeliveryEvent(event_id=event_id, event_date=edate, parcel_id=parcel_id,
                             rider_id=rider_id, event_ts=ts, event_type=event_type,
                             reason_code=reason, attempt_no=1, cod_collected=cod)

    def _next_event_id(self) -> str:
        top = 0
        for eid in self._event_ids:
            m = re.search(r"(\d+)$", eid)
            if m:
                top = max(top, int(m.group(1)))
        return f"EVT-{top + 1:05d}"

    # ---- queries -------------------------------------------------------

    @property
    def events(self) -> tuple[DeliveryEvent, ...]:
        """Every event, ordered by (event_ts, event_id number, event_id)."""
        return tuple(sorted(self._events, key=_event_sort_key))

    def __len__(self) -> int:
        return len(self._events)

    def events_for_parcel(self, parcel_id: str) -> tuple[DeliveryEvent, ...]:
        return tuple(self._by_parcel.get(parcel_id, ()))

    def events_for_rider(self, rider_id: str, event_date: date | None = None) -> tuple[DeliveryEvent, ...]:
        return self.query(rider_id=rider_id, event_date=event_date)

    def query(self, *, parcel_id: str | None = None, rider_id: str | None = None,
              event_date: date | None = None,
              event_types: Iterable[str] | None = None) -> tuple[DeliveryEvent, ...]:
        """Filter events (all given filters must match), deterministic order.

        ``event_date`` is the event's operating date (the ``event_date`` column).
        """
        types = frozenset(event_types) if event_types is not None else None
        out = [e for e in (self._by_parcel.get(parcel_id, []) if parcel_id is not None else self._events)
               if (rider_id is None or e.rider_id == rider_id)
               and (event_date is None or e.event_date == event_date)
               and (types is None or e.event_type in types)]
        return tuple(sorted(out, key=_event_sort_key))

    def flagged_events(self) -> tuple[tuple[DeliveryEvent, str], ...]:
        """FAILED events whose reason is not NOT_HOME / WRONG_ADDRESS / RIDER_FAULT.

        Informational only (e.g. the sample's CUSTOMER_BUSY). Payout logic
        decides what, if anything, such an event earns.
        """
        return tuple((e, FLAG_UNRECOGNISED_REASON) for e in self.events
                     if e.event_type == FAILED and e.reason_code not in FAILURE_REASONS)

    # ---- lifecycle -----------------------------------------------------

    def lifecycle(self, parcel_id: str, *, before: date | None = None) -> ParcelLifecycle:
        """Derived lifecycle of one parcel.

        With ``before=D`` only events with ``event_date < D`` are used, i.e. the
        parcel's state at the START of operating day D.
        """
        parcel = self._parcels.get(parcel_id)
        if parcel is None:
            raise KeyError(f"unknown parcel_id {parcel_id!r}")
        evs = self._by_parcel.get(parcel_id, [])
        if before is not None:
            evs = [e for e in evs if e.event_date < before]
        return _walk_history(parcel, list(evs))

    def all_lifecycles(self) -> dict[str, ParcelLifecycle]:
        return {pid: self.lifecycle(pid) for pid in sorted(self._parcels)}

    def rto_parcel_ids(self) -> tuple[str, ...]:
        return tuple(pid for pid, lc in self.all_lifecycles().items() if lc.is_rto)

    def pool_for_date(self, pool_date: date) -> tuple[Parcel, ...]:
        """Parcels in the delivery pool on ``pool_date`` (sorted by parcel_id).

        = fresh parcels whose ``pool_date`` is that day, plus parcels
        rescheduled on the previous day (priority already upgraded; returned as
        ``dataclasses.replace`` copies, so loaded Parcels are untouched). RTO
        and delivered parcels are excluded. This module does not re-pool parcels
        that were never attempted or that FAILED without a reschedule.
        """
        pool: list[Parcel] = []
        for pid in sorted(self._parcels):
            parcel = self._parcels[pid]
            lc = self.lifecycle(pid, before=pool_date)
            if lc.status == STATUS_PENDING and parcel.pool_date == pool_date:
                pool.append(parcel)
            elif lc.status == STATUS_RESCHEDULED and lc.next_eligible_date == pool_date:
                pool.append(replace(parcel, priority=lc.current_priority))
        return tuple(pool)


def load_event_log(data_dir: str | Path) -> EventLog:
    """Load the CSVs with the existing loader and validate the event history.

    Raises ``DataValidationError`` for loader problems or lifecycle contradictions.
    Input files are only read, never written.
    """
    return EventLog.from_dataset(load_dataset(data_dir))
