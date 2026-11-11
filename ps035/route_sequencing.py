
"""Route sequencing for ONE rider's already-assigned parcels (BR-02/03/04).

Beginner notes
--------------
This module decides the ORDER of stops for a single rider. It does not choose
which rider gets which parcel, and it does not calculate pay.

Algorithm: STRICT-PRIORITY greedy nearest-insertion (BR-02). The final route
is always [all sequenced EXPRESS stops] + [all sequenced STANDARD stops].

1. Split the parcels into EXPRESS and STANDARD groups. Inside each group,
   earlier window_end comes first, then parcel_id.
2. Start with an empty route (hub -> hub).
3. Insert each parcel only inside its own priority block.
4. Recalculate the route and check all time windows, EXPRESS first-stop
   cutoff, and return-to-hub time.
5. Choose the feasible insertion with the smallest added distance.
6. If no position is feasible, report the parcel as unsequenced.

Input rules: structural problems raise ValueError or TypeError.
Capacity (BR-01) is checked by the assignment module, not here.

Times are timedelta since midnight. Results are never wrapped past midnight.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Iterable

from ps035.distance import haversine_km
from ps035.eta import (
    calculate_stop_timing, format_hhmmss, is_return_allowed, service_duration,
    time_of_day, travel_duration,
)
from ps035.loader import Parcel

EXPRESS_FIRST_STOP_CUTOFF = time_of_day("09:30:00")
_DAY = timedelta(hours=24)

LATE_FOR_WINDOW = "LATE_FOR_WINDOW"
EXPRESS_FIRST_STOP_AFTER_0930 = "EXPRESS_FIRST_STOP_AFTER_0930"
RETURN_AFTER_SHIFT_END_PLUS_60 = "RETURN_AFTER_SHIFT_END_PLUS_60"


@dataclass(frozen=True)
class RouteStop:
    """One planned stop (times are timedeltas since midnight)."""

    seq: int
    parcel_id: str
    from_id: str
    leg_km: float
    priority: str
    is_cod: bool
    window_start: timedelta
    window_end: timedelta
    arrival: timedelta
    wait: timedelta
    eta: timedelta
    service: timedelta
    departure: timedelta


@dataclass(frozen=True)
class UnsequencedParcel:
    """A parcel that could not be placed, with the reason."""

    parcel_id: str
    reason_code: str
    detail: str


@dataclass(frozen=True)
class RouteResult:
    """Planned route for one rider."""

    hub_id: str
    shift_start: timedelta
    shift_end: timedelta
    stops: tuple[RouteStop, ...]
    return_from_id: str
    return_leg_km: float
    return_time: timedelta
    total_km: float
    uses_overtime: bool
    unsequenced: tuple[UnsequencedParcel, ...]

    @property
    def status(self) -> str:
        """Return COMPLETE if every parcel was sequenced, else PARTIAL."""
        return "COMPLETE" if not self.unsequenced else "PARTIAL"

    @property
    def parcel_ids(self) -> tuple[str, ...]:
        return tuple(s.parcel_id for s in self.stops)


# --------------------------------------------------------------------------
# Input validation
# --------------------------------------------------------------------------

def _validate_parcels(parcels: Iterable[Parcel], hub_id: str) -> list[Parcel]:
    if isinstance(parcels, (str, bytes)):
        raise TypeError("parcels must be a collection of Parcel records")

    items = list(parcels)
    seen: set[str] = set()

    for p in items:
        if not isinstance(p, Parcel):
            raise TypeError(
                f"each parcel must be a loader Parcel record, "
                f"got {type(p).__name__}"
            )

        pid = p.parcel_id

        # Validate parcel ID before using it in sorting or distance calculations.
        if not isinstance(pid, str):
            raise ValueError(
                f"parcel_id must be a non-empty string, got {pid!r}"
            )

        if not pid.strip():
            raise ValueError("parcel_id must be a non-empty string")

        if pid in seen:
            raise ValueError(f"duplicate parcel_id {pid!r}")

        if pid == hub_id:
            raise ValueError(f"parcel_id {pid!r} clashes with the hub id")

        seen.add(pid)

        if p.priority not in ("EXPRESS", "STANDARD"):
            raise ValueError(
                f"{pid}: priority must be EXPRESS or STANDARD, "
                f"got {p.priority!r}"
            )

        if not isinstance(p.cod_flag, bool):
            raise TypeError(
                f"{pid}: cod_flag must be a bool, "
                f"got {type(p.cod_flag).__name__}"
            )

        try:
            haversine_km(
                p.dest_lat, p.dest_lon, p.dest_lat, p.dest_lon
            )
            start = time_of_day(p.window_start)
            end = time_of_day(p.window_end)
        except (TypeError, ValueError) as exc:
            raise type(exc)(f"{pid}: {exc}") from None

        if end < start:
            raise ValueError(f"{pid}: window_end is before window_start")

    return items


# --------------------------------------------------------------------------
# The planner
# --------------------------------------------------------------------------

def _fmt(value: timedelta) -> str:
    try:
        return format_hhmmss(value)
    except ValueError:
        return str(value)


class _Failure:
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail


def sequence_route(
    hub_lat,
    hub_lon,
    shift_start,
    shift_end,
    parcels: Iterable[Parcel],
    hub_id: str = "HUB",
) -> RouteResult:
    """Sequence one rider's parcels using strict-priority greedy insertion."""

    start = time_of_day(shift_start)
    end = time_of_day(shift_end)

    if end < start:
        raise ValueError("shift_end is before shift_start")

    haversine_km(hub_lat, hub_lon, hub_lat, hub_lon)
    items = _validate_parcels(parcels, hub_id)

    coords = {hub_id: (hub_lat, hub_lon)}
    coords.update({
        p.parcel_id: (p.dest_lat, p.dest_lon)
        for p in items
    })

    cache: dict[tuple[str, str], float] = {}

    def km(a: str, b: str) -> float:
        if a == b:
            return 0.0

        key = (a, b) if a < b else (b, a)

        if key not in cache:
            cache[key] = haversine_km(*coords[a], *coords[b])

        return cache[key]

    def dec(x: float) -> Decimal:
        return Decimal(repr(x))

    def check(
        order: list[Parcel],
        prefix: list,
    ) -> tuple[list, float, timedelta] | _Failure:
        """Recalculate the route from the first changed stop."""

        timings = list(prefix)
        depart = timings[-1].departure if timings else start
        prev = order[len(timings) - 1].parcel_id if timings else hub_id

        for idx in range(len(timings), len(order)):
            p = order[idx]

            if depart >= _DAY:
                return _Failure(
                    LATE_FOR_WINDOW,
                    f"{p.parcel_id}: rider would leave the previous stop "
                    "at or after 24:00:00",
                )

            t = calculate_stop_timing(
                depart,
                km(prev, p.parcel_id),
                p.window_start,
                p.window_end,
                p.cod_flag,
            )

            if not t.within_window:
                return _Failure(
                    LATE_FOR_WINDOW,
                    f"{p.parcel_id}: ETA {_fmt(t.eta)} is after "
                    f"window_end {_fmt(time_of_day(p.window_end))}",
                )

            if (
                idx == 0
                and p.priority == "EXPRESS"
                and t.eta > EXPRESS_FIRST_STOP_CUTOFF
            ):
                return _Failure(
                    EXPRESS_FIRST_STOP_AFTER_0930,
                    f"{p.parcel_id}: EXPRESS first-stop ETA "
                    f"{_fmt(t.eta)} is after 09:30:00",
                )

            timings.append(t)
            depart = t.departure
            prev = p.parcel_id

        back_km = km(prev, hub_id)
        back = depart + travel_duration(back_km)

        if not is_return_allowed(back, end):
            return _Failure(
                RETURN_AFTER_SHIFT_END_PLUS_60,
                f"return to hub at {_fmt(back)} is after "
                f"shift_end + 60 min "
                f"({_fmt(end + timedelta(minutes=60))})",
            )

        return timings, back_km, back

    # Strict priority: EXPRESS first, then STANDARD.
    def processing_key(p: Parcel) -> tuple:
        return (time_of_day(p.window_end), p.parcel_id)

    express_todo = sorted(
        (p for p in items if p.priority == "EXPRESS"),
        key=processing_key,
    )

    standard_todo = sorted(
        (p for p in items if p.priority == "STANDARD"),
        key=processing_key,
    )

    order: list[Parcel] = []
    timings: list = []
    unsequenced: list[UnsequencedParcel] = []
    express_count = 0

    for parcel in express_todo + standard_todo:
        pid = parcel.parcel_id
        is_express = parcel.priority == "EXPRESS"

        first_pos = 0 if is_express else express_count
        last_pos = express_count if is_express else len(order)

        candidates = []

        for pos in range(first_pos, last_pos + 1):
            before = order[pos - 1].parcel_id if pos > 0 else hub_id
            after = order[pos].parcel_id if pos < len(order) else hub_id

            added = (
                dec(km(before, pid))
                + dec(km(pid, after))
                - dec(km(before, after))
            )

            trial = order[:pos] + [parcel] + order[pos:]
            candidates.append((added, pos, check(trial, timings[:pos])))

        candidates.sort(key=lambda c: (c[0], c[1]))

        feasible = [
            c for c in candidates
            if not isinstance(c[2], _Failure)
        ]

        if feasible:
            _, pos, (new_timings, _, _) = feasible[0]
            order.insert(pos, parcel)
            timings = new_timings

            if is_express:
                express_count += 1

        else:
            best = candidates[0][2]
            codes = sorted({c[2].code for c in candidates})

            unsequenced.append(
                UnsequencedParcel(
                    pid,
                    best.code,
                    f"no feasible insertion among {len(candidates)} "
                    f"position(s); cheapest position: {best.detail}"
                    f" (failure types seen: {', '.join(codes)})",
                )
            )

    # Internal safety net: enforce strict-priority route shape.
    if (
        any(p.priority != "EXPRESS" for p in order[:express_count])
        or any(
            p.priority != "STANDARD"
            for p in order[express_count:]
        )
    ):
        raise RuntimeError(
            "internal error: route is not [all EXPRESS] + [all STANDARD]"
        )

    # Build the final result.
    stops: list[RouteStop] = []
    prev = hub_id
    legs: list[float] = []

    for i, (p, t) in enumerate(zip(order, timings), start=1):
        leg = km(prev, p.parcel_id)
        legs.append(leg)

        stops.append(
            RouteStop(
                seq=i,
                parcel_id=p.parcel_id,
                from_id=prev,
                leg_km=leg,
                priority=p.priority,
                is_cod=p.cod_flag,
                window_start=time_of_day(p.window_start),
                window_end=time_of_day(p.window_end),
                arrival=t.arrival,
                wait=t.wait,
                eta=t.eta,
                service=service_duration(p.cod_flag),
                departure=t.departure,
            )
        )

        prev = p.parcel_id

    back_km = km(prev, hub_id)
    last_departure = timings[-1].departure if timings else start
    return_time = last_departure + travel_duration(back_km)

    total = (
        sum((dec(x) for x in legs), Decimal(0))
        + dec(back_km)
    )

    return RouteResult(
        hub_id=hub_id,
        shift_start=start,
        shift_end=end,
        stops=tuple(stops),
        return_from_id=prev,
        return_leg_km=back_km,
        return_time=return_time,
        
        total_km=float(total),
        uses_overtime=return_time > end,
        unsequenced=tuple(unsequenced),
    )
