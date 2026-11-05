"""ETA (estimated time of arrival) arithmetic for route legs (BR-03 / BR-04).

Beginner notes
--------------
Rules used (BR-03):
* Travel: 4 minutes per km (15 km/h).
* Service: 3 minutes per stop, plus 5 more minutes if the parcel is COD.
* If the rider arrives before ``window_start`` they WAIT until it opens.
* The ETA that must lie inside [window_start, window_end] (both ends
  allowed) is the time service STARTS. The official sample README says so:
  "window_start: ETA must be >= this", "window_end: ETA must be <= this".
  Service time is added AFTER that moment, before driving to the next stop.
* A late arrival is NEVER clamped to window_end; the stop is just reported
  as not within its window (the later planner decides what that means).

How times are stored
--------------------
A "time of day" is a ``datetime.timedelta`` counted from 00:00:00. Timedelta
keeps microseconds exactly, so 0.01 km -> 2.4 s is not lost. Loader ``time``
values and ``"HH:MM[:SS]"`` strings are converted with ``time_of_day``.

* Fractions: distance -> time is exact decimal arithmetic, rounded half-up to
  whole MICROseconds only if a distance has more than ~6 decimals.
* Window and shift checks compare the exact (unrounded) times.
* Printing (``format_hhmmss``) rounds half-up to the nearest whole second.

Midnight: input times must be 00:00:00 .. 23:59:59. Calculated times may run
past 24:00:00 (e.g. 25:10:00), which keeps comparisons correct: such a time is
simply after any window. They never wrap around to "01:10". Asking to PRINT a
time at or after 24:00:00 raises ValueError instead of printing a wrong value.

Leg distances come from ``ps035.distance.haversine_km`` (already rounded to 2
decimals); this module takes a distance as a plain number and does not
import or repeat any Haversine maths.

Errors: TypeError for the wrong kind of value (str/None/bool distance, non-bool
COD flag, ...); ValueError for bad values (NaN, infinity, negative, malformed
time text, window_end before window_start).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import time, timedelta
from decimal import ROUND_HALF_UP, Decimal

SECONDS_PER_KM = Decimal(240)               # 4 minutes per km
BASE_SERVICE = timedelta(minutes=3)
COD_EXTRA_SERVICE = timedelta(minutes=5)
MAX_OVERTIME = timedelta(minutes=60)        # BR-04
_DAY = timedelta(hours=24)
_MICROS = Decimal(1_000_000)

_TIME_RE = re.compile(r"^(\d{2}):(\d{2})(?::(\d{2}))?$")


# --------------------------------------------------------------------------
# Time parsing / formatting
# --------------------------------------------------------------------------


def time_of_day(value: str | time | timedelta) -> timedelta:
    """Convert "HH:MM", "HH:MM:SS", ``datetime.time`` or ``timedelta`` to a
    time of day (timedelta since midnight, 00:00:00 <= t < 24:00:00)."""
    if isinstance(value, bool):
        raise TypeError("time must be str, datetime.time or timedelta, got bool")
    if isinstance(value, str):
        m = _TIME_RE.match(value)
        if not m:
            raise ValueError(f"bad time {value!r}, expected HH:MM or HH:MM:SS")
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        if h > 23 or mi > 59 or s > 59:
            raise ValueError(f"time out of range: {value!r}")
        return timedelta(hours=h, minutes=mi, seconds=s)
    if isinstance(value, time):
        return timedelta(hours=value.hour, minutes=value.minute, seconds=value.second,
                         microseconds=value.microsecond)
    if isinstance(value, timedelta):
        if not timedelta(0) <= value < _DAY:
            raise ValueError(f"time of day must be 00:00:00..23:59:59, got {value}")
        return value
    raise TypeError(f"time must be str, datetime.time or timedelta, got {type(value).__name__}")


def format_hhmmss(value: timedelta) -> str:
    """Format a time as zero-padded HH:MM:SS (rounded half-up to a whole second).

    Raises ValueError if the (rounded) time is negative or 24:00:00 or later.
    """
    if not isinstance(value, timedelta):
        raise TypeError(f"expected timedelta, got {type(value).__name__}")
    micros = value // timedelta(microseconds=1)
    total = (micros + 500_000) // 1_000_000  # half-up for non-negative values
    if micros < 0 or total >= 24 * 3600:
        raise ValueError(f"cannot print {value} as a time of day (crosses midnight or negative)")
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# --------------------------------------------------------------------------
# Durations
# --------------------------------------------------------------------------


def travel_duration(distance_km: float | Decimal) -> timedelta:
    """Time to drive ``distance_km`` at 4 min/km. Distance is used as given
    (never re-rounded); it must be finite and not negative."""
    if isinstance(distance_km, bool) or not isinstance(distance_km, (int, float, Decimal)):
        raise TypeError(f"distance_km must be a number, got {type(distance_km).__name__}: {distance_km!r}")
    if isinstance(distance_km, float):
        if not math.isfinite(distance_km):
            raise ValueError(f"distance_km must be finite, got {distance_km!r}")
        exact = Decimal(repr(distance_km))
    else:
        exact = Decimal(distance_km)
        if not exact.is_finite():
            raise ValueError(f"distance_km must be finite, got {distance_km!r}")
    if exact < 0:
        raise ValueError(f"distance_km must not be negative, got {distance_km!r}")
    micros = (exact * SECONDS_PER_KM * _MICROS).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return timedelta(microseconds=int(micros))


def service_duration(is_cod: bool) -> timedelta:
    """3 minutes per stop; 8 minutes (3 + 5) for a COD parcel."""
    if not isinstance(is_cod, bool):
        raise TypeError(f"is_cod must be a bool, got {type(is_cod).__name__}: {is_cod!r}")
    return BASE_SERVICE + COD_EXTRA_SERVICE if is_cod else BASE_SERVICE


# --------------------------------------------------------------------------
# One stop
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StopTiming:
    """Timing of one stop. All times are timedeltas since midnight."""

    arrival: timedelta        # departure + travel
    wait: timedelta           # max(0, window_start - arrival)
    eta: timedelta            # service start = max(arrival, window_start)
    departure: timedelta      # eta + service duration (leaving this stop)
    within_window: bool       # window_start <= eta <= window_end (inclusive)


def is_within_window(when: str | time | timedelta, window_start: str | time | timedelta,
                     window_end: str | time | timedelta) -> bool:
    """True if window_start <= when <= window_end (both ends allowed)."""
    start, end = time_of_day(window_start), time_of_day(window_end)
    if end < start:
        raise ValueError("window_end is before window_start")
    w = when if isinstance(when, timedelta) and when >= _DAY else time_of_day(when)
    return start <= w <= end


def calculate_stop_timing(depart_from_previous: str | time | timedelta, leg_km: float | Decimal,
                          window_start: str | time | timedelta, window_end: str | time | timedelta,
                          is_cod: bool) -> StopTiming:
    """Work out arrival, wait, ETA and next departure for one stop.

    ``depart_from_previous`` is when the rider leaves the previous stop (or the
    hub at shift start); ``leg_km`` is the Haversine leg distance.
    A late stop is returned with ``within_window=False`` (not clamped).
    """
    depart = time_of_day(depart_from_previous)
    start, end = time_of_day(window_start), time_of_day(window_end)
    if end < start:
        raise ValueError("window_end is before window_start")
    service = service_duration(is_cod)

    arrival = depart + travel_duration(leg_km)
    eta = max(arrival, start)
    return StopTiming(
        arrival=arrival,
        wait=max(timedelta(0), start - arrival),
        eta=eta,
        departure=eta + service,
        within_window=eta <= end,   # eta >= start is always true after waiting
    )


# --------------------------------------------------------------------------
# Shift end (BR-04 boundary helper only; no payout / overtime pay here)
# --------------------------------------------------------------------------


def is_return_allowed(return_time: timedelta, shift_end: str | time | timedelta) -> bool:
    """True if a hub return is no later than shift_end + 60 minutes (inclusive).

    Exactly shift_end and exactly shift_end + 60:00 are both allowed;
    one second later than that is not. ``return_time`` may be past midnight.
    """
    if not isinstance(return_time, timedelta):
        raise TypeError(f"return_time must be a timedelta, got {type(return_time).__name__}")
    return return_time <= time_of_day(shift_end) + MAX_OVERTIME
