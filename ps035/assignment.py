"""Rider-to-parcel assignment for one day's pool (BR-01, BR-02).

Beginner notes
--------------
This module decides WHICH rider gets WHICH parcel. It does not decide the stop
order or the ETAs itself: every candidate rider route is built by the existing
``ps035.route_sequencing.sequence_route`` (nearest insertion, strict EXPRESS
priority, windows, 09:30 EXPRESS rule, return by shift_end + 60 min). Nothing
from Haversine/ETA/route sequencing is re-implemented here. It does not
calculate pay and it is not wired into the command runner.

Strategy (documented in the Week 1 design document, BR-02 and the workflow
diagram: "try eligible riders in ascending added-distance order; if none is
feasible, leave UNASSIGNED"):

1. Validate riders, hubs, shifts and parcels (bad data raises ValueError /
   TypeError; it is not "unassigned").
2. Process parcels in priority order: EXPRESS first, then STANDARD; inside each
   tier earlier ``window_end`` first, then ``parcel_id``.
3. For each parcel look at every rostered rider in ``rider_id`` order. A rider
   is a candidate only if BOTH capacity limits still hold after adding the
   parcel (count and total kg, exact ``Decimal`` arithmetic, exactly-at-limit is
   allowed).
4. For each candidate rider, ``sequence_route`` is called with that rider's
   current parcels PLUS the new parcel. The rider is feasible only if the
   result sequences ALL of them (nothing unsequenced). A parcel that would
   push an already-assigned parcel out of the route is therefore refused.
5. Among feasible riders choose the lowest ADDED route distance
   (new route ``total_km`` minus the rider's current ``total_km``, return leg
   included). Equal added distance -> lowest ``rider_id``.
6. No feasible rider -> the parcel goes to ``unassigned`` with a reason code.
7. Every rider's final ``RouteResult`` is the sequencer's answer for the
   rider's final parcel set.

Difference from the fallback heuristic in the task brief: the brief suggested
"lowest resulting TOTAL route distance"; the design document says "lowest ADDED
route distance", so the design document is followed. (Total and added only
differ when riders already carry different amounts of work.)

Capacity (BR-01): BIKE and EV_BIKE 40 parcels / 25 kg, VAN 120 parcels / 300 kg.
The vehicle type fixes the limit; if a rider record carries a SMALLER
``capacity_parcels`` / ``capacity_kg`` the smaller value is used, so no limit
is ever exceeded.

Rostering (sample-data README: "a missing row = day off; shift rows override
the default shift"): pass ``shifts`` and ``plan_date`` to use the rider's shift
for that date; riders without a row are listed in ``off_roster_rider_ids`` and
get no parcels. Without ``shifts`` the rider's default shift is used.

Unassigned reason codes. Route reasons reuse the route-sequencing constants:
``LATE_FOR_WINDOW``, ``EXPRESS_FIRST_STOP_AFTER_0930``,
``RETURN_AFTER_SHIFT_END_PLUS_60``; plus ``NO_RIDER_CAPACITY`` (every rider is
full), ``NO_FEASIBLE_ROUTE`` (riders failed for different/mixed reasons, or the
parcel would displace an already-assigned one) and ``NO_RIDER_AVAILABLE``
(no rostered rider at all). The ``detail`` text lists each rider's outcome.

Limitation: this is a greedy heuristic, not a globally optimal fleet-routing
solver. An early choice is never revisited, so a different overall split of the
parcels might serve more of them. Results are deterministic and independent of
input order.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal
from typing import Iterable

from ps035.distance import haversine_km
from ps035.eta import time_of_day
from ps035.loader import Hub, Parcel, Rider, RiderShift
from ps035.route_sequencing import RouteResult, sequence_route

# Capacity limits per vehicle type (BR-01): (max parcels, max kg)
VEHICLE_LIMITS: dict[str, tuple[int, Decimal]] = {
    "BIKE": (40, Decimal("25.0")),
    "EV_BIKE": (40, Decimal("25.0")),
    "VAN": (120, Decimal("300.0")),
}

NO_RIDER_CAPACITY = "NO_RIDER_CAPACITY"
NO_FEASIBLE_ROUTE = "NO_FEASIBLE_ROUTE"
NO_RIDER_AVAILABLE = "NO_RIDER_AVAILABLE"

_CAPACITY_OUTCOME = "CAPACITY"


# --------------------------------------------------------------------------
# Result records
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RiderAssignment:
    """One rostered rider's parcels, final route and capacity position."""

    rider_id: str
    hub_id: str
    shift_start: time
    shift_end: time
    parcel_ids: tuple[str, ...]      # in route (stop) order
    route: RouteResult
    parcel_count: int
    total_weight_kg: Decimal
    capacity_parcels: int
    capacity_kg: Decimal

    @property
    def remaining_parcels(self) -> int:
        return self.capacity_parcels - self.parcel_count

    @property
    def remaining_kg(self) -> Decimal:
        return self.capacity_kg - self.total_weight_kg


@dataclass(frozen=True)
class UnassignedParcel:
    parcel_id: str
    reason_code: str
    detail: str


@dataclass(frozen=True)
class AssignmentResult:
    """Outcome for the whole fleet (every list is sorted deterministically)."""

    assignments: tuple[RiderAssignment, ...]        # rostered riders, by rider_id
    unassigned: tuple[UnassignedParcel, ...]        # in processing order
    off_roster_rider_ids: tuple[str, ...]           # riders with no shift that day

    @property
    def assigned_parcel_ids(self) -> tuple[str, ...]:
        return tuple(pid for a in self.assignments for pid in a.parcel_ids)

    @property
    def unassigned_parcel_ids(self) -> tuple[str, ...]:
        return tuple(u.parcel_id for u in self.unassigned)

    @property
    def status(self) -> str:
        """COMPLETE (nothing unassigned, incl. no parcels), PARTIAL, or NONE_ASSIGNED."""
        if not self.unassigned:
            return "COMPLETE"
        return "PARTIAL" if self.assigned_parcel_ids else "NONE_ASSIGNED"

    def for_rider(self, rider_id: str) -> RiderAssignment | None:
        for a in self.assignments:
            if a.rider_id == rider_id:
                return a
        return None


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------


def vehicle_capacity(vehicle_type: str) -> tuple[int, Decimal]:
    """BR-01 limits (max parcels, max kg) for a vehicle type."""
    try:
        return VEHICLE_LIMITS[vehicle_type]
    except (KeyError, TypeError):
        raise ValueError(
            f"unknown vehicle_type {vehicle_type!r}; expected one of {', '.join(VEHICLE_LIMITS)}") from None


def _to_decimal(value: object, label: str) -> Decimal:
    """Convert a weight/limit to a finite, non-negative Decimal (never rounded)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError(f"{label} must be a number, got {type(value).__name__}: {value!r}")
    d = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    if not d.is_finite():
        raise ValueError(f"{label} must be a finite number, got {value!r}")
    if d < 0:
        raise ValueError(f"{label} must not be negative, got {value!r}")
    return d


def _list_of(records: Iterable, cls: type, name: str) -> list:
    if isinstance(records, (str, bytes)):
        raise TypeError(f"{name} must be a collection of {cls.__name__} records")
    items = list(records)
    for r in items:
        if not isinstance(r, cls):
            raise TypeError(f"each item of {name} must be a loader {cls.__name__} record, got {type(r).__name__}")
    return items


def _validate_parcels(parcels: Iterable[Parcel]) -> list[tuple[Parcel, Decimal]]:
    items = _list_of(parcels, Parcel, "parcels")
    seen: set[str] = set()
    out = []
    for p in items:
        if p.parcel_id in seen:
            raise ValueError(f"duplicate parcel_id {p.parcel_id!r}")
        seen.add(p.parcel_id)
        if p.priority not in ("EXPRESS", "STANDARD"):
            raise ValueError(f"{p.parcel_id}: priority must be EXPRESS or STANDARD, got {p.priority!r}")
        if not isinstance(p.cod_flag, bool):
            raise TypeError(f"{p.parcel_id}: cod_flag must be a bool, got {type(p.cod_flag).__name__}")
        weight = _to_decimal(p.weight_kg, f"{p.parcel_id}: weight_kg")
        try:
            haversine_km(p.dest_lat, p.dest_lon, p.dest_lat, p.dest_lon)  # validates coordinates
            start, end = time_of_day(p.window_start), time_of_day(p.window_end)
        except (TypeError, ValueError) as exc:
            raise type(exc)(f"{p.parcel_id}: {exc}") from None
        if end < start:
            raise ValueError(f"{p.parcel_id}: window_end is before window_start")
        out.append((p, weight))
    return out


@dataclass
class _RiderState:
    """Working state for one rostered rider while assigning."""

    rider: Rider
    hub: Hub
    shift_start: time
    shift_end: time
    cap_parcels: int
    cap_kg: Decimal
    parcels: list[Parcel]
    weight: Decimal
    route: RouteResult


def _route_for(state: _RiderState, parcels: list[Parcel]) -> RouteResult:
    return sequence_route(state.hub.lat, state.hub.lon, state.shift_start, state.shift_end,
                          parcels, hub_id=state.hub.hub_id)


def _build_riders(riders: list[Rider], hubs: list[Hub], shifts: list[RiderShift] | None,
                  plan_date: date | None) -> tuple[list[_RiderState], list[str]]:
    hub_by_id: dict[str, Hub] = {}
    for h in hubs:
        if h.hub_id in hub_by_id:
            raise ValueError(f"duplicate hub_id {h.hub_id!r}")
        hub_by_id[h.hub_id] = h

    shift_by_key: dict[tuple[str, date], RiderShift] = {}
    if shifts is not None:
        if plan_date is None:
            raise ValueError("plan_date is required when shifts are supplied")
        if not isinstance(plan_date, date):
            raise TypeError("plan_date must be a datetime.date")
        for s in shifts:
            key = (s.rider_id, s.shift_date)
            if key in shift_by_key:
                raise ValueError(f"duplicate shift for {s.rider_id} on {s.shift_date}")
            shift_by_key[key] = s
    elif plan_date is not None:
        raise ValueError("plan_date is only meaningful together with shifts")

    seen: set[str] = set()
    states: list[_RiderState] = []
    off_roster: list[str] = []
    for r in sorted(riders, key=lambda x: x.rider_id):
        if r.rider_id in seen:
            raise ValueError(f"duplicate rider_id {r.rider_id!r}")
        seen.add(r.rider_id)
        max_parcels, max_kg = vehicle_capacity(r.vehicle_type)
        if isinstance(r.capacity_parcels, bool) or not isinstance(r.capacity_parcels, int) or r.capacity_parcels <= 0:
            raise ValueError(f"{r.rider_id}: capacity_parcels must be a positive whole number, got {r.capacity_parcels!r}")
        rec_kg = _to_decimal(r.capacity_kg, f"{r.rider_id}: capacity_kg")
        hub = hub_by_id.get(r.home_hub)
        if hub is None:
            raise ValueError(f"{r.rider_id}: home_hub {r.home_hub!r} not found in the supplied hubs")
        if shifts is None:
            start, end = r.default_shift_start, r.default_shift_end
        else:
            s = shift_by_key.get((r.rider_id, plan_date))
            if s is None:
                off_roster.append(r.rider_id)
                continue
            start, end = s.shift_start, s.shift_end
        if time_of_day(end) < time_of_day(start):
            raise ValueError(f"{r.rider_id}: shift_end is before shift_start")
        state = _RiderState(
            rider=r, hub=hub, shift_start=start, shift_end=end,
            cap_parcels=min(max_parcels, r.capacity_parcels), cap_kg=min(max_kg, rec_kg),
            parcels=[], weight=Decimal(0), route=None)  # type: ignore[arg-type]
        state.route = _route_for(state, [])
        states.append(state)
    return states, off_roster


# --------------------------------------------------------------------------
# The assigner
# --------------------------------------------------------------------------


def assign_parcels(riders: Iterable[Rider], hubs: Iterable[Hub], parcels: Iterable[Parcel],
                   shifts: Iterable[RiderShift] | None = None,
                   plan_date: date | None = None) -> AssignmentResult:
    """Assign parcels to riders by greedy lowest-added-distance (see module doc).

    riders / hubs / parcels: loader ``Rider``, ``Hub`` and ``Parcel`` records.
    shifts + plan_date: optional roster. With them, a rider's shift for
        ``plan_date`` is used and riders with no row are off roster. Without
        them every rider works the default shift.
    """
    rider_list = _list_of(riders, Rider, "riders")
    hub_list = _list_of(hubs, Hub, "hubs")
    shift_list = None if shifts is None else _list_of(shifts, RiderShift, "shifts")
    items = _validate_parcels(parcels)
    states, off_roster = _build_riders(rider_list, hub_list, shift_list, plan_date)

    # EXPRESS first; inside a tier earlier window_end, then parcel_id.
    todo = sorted(items, key=lambda it: (it[0].priority != "EXPRESS", time_of_day(it[0].window_end),
                                         it[0].parcel_id))
    unassigned: list[UnassignedParcel] = []

    for parcel, weight in todo:
        pid = parcel.parcel_id
        best: tuple[Decimal, str, _RiderState, RouteResult] | None = None
        outcomes: list[tuple[str, str, str]] = []  # (rider_id, code, text)
        for st in states:  # already sorted by rider_id
            rid = st.rider.rider_id
            if len(st.parcels) + 1 > st.cap_parcels:
                outcomes.append((rid, _CAPACITY_OUTCOME, f"parcel limit {st.cap_parcels} reached"))
                continue
            if st.weight + weight > st.cap_kg:
                outcomes.append((rid, _CAPACITY_OUTCOME,
                                 f"{st.weight} kg + {weight} kg exceeds {st.cap_kg} kg"))
                continue
            trial = _route_for(st, st.parcels + [parcel])
            if not trial.unsequenced:
                added = Decimal(repr(trial.total_km)) - Decimal(repr(st.route.total_km))
                if best is None or (added, rid) < (best[0], best[1]):
                    best = (added, rid, st, trial)
                continue
            own = next((u for u in trial.unsequenced if u.parcel_id == pid), None)
            if own is not None:
                outcomes.append((rid, own.reason_code, own.detail))
            else:
                displaced = ", ".join(u.parcel_id for u in trial.unsequenced)
                outcomes.append((rid, NO_FEASIBLE_ROUTE, f"would displace already-assigned {displaced}"))
        if best is not None:
            _, _, st, trial = best
            st.parcels.append(parcel)
            st.weight += weight
            st.route = trial
        else:
            unassigned.append(_explain(pid, outcomes))

    assignments = tuple(RiderAssignment(
        rider_id=st.rider.rider_id, hub_id=st.hub.hub_id, shift_start=st.shift_start, shift_end=st.shift_end,
        parcel_ids=st.route.parcel_ids, route=st.route, parcel_count=len(st.parcels),
        total_weight_kg=st.weight, capacity_parcels=st.cap_parcels, capacity_kg=st.cap_kg) for st in states)
    return AssignmentResult(assignments=assignments, unassigned=tuple(unassigned),
                            off_roster_rider_ids=tuple(off_roster))


def _explain(parcel_id: str, outcomes: list[tuple[str, str, str]]) -> UnassignedParcel:
    """Turn the per-rider refusals into one reason code plus a readable detail."""
    if not outcomes:
        return UnassignedParcel(parcel_id, NO_RIDER_AVAILABLE, "no rostered rider is available")
    detail = "; ".join(f"{rid}: {code} ({text})" for rid, code, text in outcomes)
    route_codes = {code for _, code, _ in outcomes if code != _CAPACITY_OUTCOME}
    if not route_codes:
        return UnassignedParcel(parcel_id, NO_RIDER_CAPACITY, detail)
    if len(route_codes) == 1:
        return UnassignedParcel(parcel_id, route_codes.pop(), detail)
    return UnassignedParcel(parcel_id, NO_FEASIBLE_ROUTE, detail)
