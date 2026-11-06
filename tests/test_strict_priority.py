"""Strict EXPRESS-first priority (BR-02): final route = [all EXPRESS stops] + [all STANDARD stops].

Additive regression tests across BOTH layers:
* ``ps035.route_sequencing.sequence_route`` owns the stop order;
* ``ps035.assignment.assign_parcels`` must pass that order through unchanged.

Geometry: the hub is at (0, 0) and ``east``/``north`` are kilometres, so Haversine legs round to the
intended lengths. Windows are wide unless a test says otherwise.
"""
from __future__ import annotations

import math
import random
from datetime import date, time
from decimal import Decimal

import pytest

from ps035.assignment import assign_parcels
from ps035.distance import haversine_km
from ps035.eta import time_of_day
from ps035.loader import Hub, Parcel, Rider
from ps035.route_sequencing import (
    EXPRESS_FIRST_STOP_AFTER_0930, LATE_FOR_WINDOW, sequence_route,
)

K = 360 / (2 * math.pi * 6371)          # degrees per km
T = time_of_day
HUB = Hub("HUB-01", "Test Hub", Decimal("0"), Decimal("0"))


def parcel(pid, east=0.0, north=0.0, ws="08:00:00", we="12:00:00", priority="STANDARD", cod=False, kg="1.0"):
    return Parcel(
        parcel_id=pid, pool_date=date(2026, 10, 12),
        dest_lat=Decimal(str(round(north * K, 9))), dest_lon=Decimal(str(round(east * K, 9))),
        weight_kg=Decimal(kg), window_start=time.fromisoformat(ws), window_end=time.fromisoformat(we),
        priority=priority, cod_flag=cod, cod_amount=Decimal("100.00") if cod else Decimal("0.00"))


def route(parcels, start="08:00:00", end="18:00:00"):
    return sequence_route(0, 0, start, end, parcels)


def rider(rid="RDR-01", vehicle="BIKE", start="08:00:00", end="18:00:00"):
    cap_p, cap_kg = {"BIKE": (40, "25.0"), "EV_BIKE": (40, "25.0"), "VAN": (120, "300.0")}[vehicle]
    return Rider(rider_id=rid, rider_name=rid, vehicle_type=vehicle, capacity_parcels=cap_p,
                 capacity_kg=Decimal(cap_kg), home_hub="HUB-01",
                 default_shift_start=time.fromisoformat(start), default_shift_end=time.fromisoformat(end))


def tour_km(parcels_by_id, order):
    pts = {i: (p.dest_lat, p.dest_lon) for i, p in parcels_by_id.items()}
    path = [(0, 0)] + [pts[i] for i in order] + [(0, 0)]
    return sum(haversine_km(*a, *b) for a, b in zip(path, path[1:]))


def assert_strict(stops):
    """Every EXPRESS stop precedes every STANDARD stop; an EXPRESS first stop never arrives after 09:30.
    An empty route is trivially fine."""
    kinds = [s.priority for s in stops]
    assert kinds == sorted(kinds, key=lambda k: k != "EXPRESS"), kinds
    if "EXPRESS" in kinds:
        assert kinds[0] == "EXPRESS" and stops[0].eta <= T("09:30:00")


def oracle_order(parcels):
    """INDEPENDENT re-derivation of the documented algorithm for problems with no time pressure:
    EXPRESS group first (earlier window_end, then parcel_id), then STANDARD group; each parcel goes to the position
    of its own block with the smallest added Haversine distance, ties -> earlier position; STANDARD only at/after
    the end of the EXPRESS block. (Valid only when every window/shift constraint is slack.)"""
    pts = {p.parcel_id: (p.dest_lat, p.dest_lon) for p in parcels}

    def d(a, b):
        pa = (0, 0) if a == "HUB" else pts[a]
        pb = (0, 0) if b == "HUB" else pts[b]
        return Decimal(repr(haversine_km(*pa, *pb)))

    order, boundary = [], 0
    for priority in ("EXPRESS", "STANDARD"):
        group = sorted((p for p in parcels if p.priority == priority), key=lambda p: (time_of_day(p.window_end), p.parcel_id))
        for p in group:
            best = None
            for pos in range(boundary, len(order) + 1):
                before = order[pos - 1] if pos > 0 else "HUB"
                after = order[pos] if pos < len(order) else "HUB"
                cost = d(before, p.parcel_id) + d(p.parcel_id, after) - d(before, after)
                if best is None or cost < best[0]:      # strict '<': a tie keeps the EARLIER position
                    best = (cost, pos)
            order.insert(best[1], p.parcel_id)
        boundary = len(order)
    return tuple(order)


# --------------------------------------------------------------------------------------------
# Nearest insertion INSIDE the EXPRESS group
# --------------------------------------------------------------------------------------------

# Processing order is by window_end: P1 (7 E, 4 N), P2 (2 W, 5 S), P3 (7 E, 8 S). Insertion steps:
# [P1]; P2 ties at both ends -> earlier position -> [P2, P1]; P3 is cheapest BETWEEN P2 and P1 -> [P2, P3, P1].
# Sorting by distance from the hub would give [P2, P1, P3], a longer tour (40.75 km vs 34.94 km).
EXPRESS_SET = [
    parcel("P1", 7, 4, we="10:00:00", priority="EXPRESS"),
    parcel("P2", -2, -5, we="11:00:00", priority="EXPRESS"),
    parcel("P3", 7, -8, we="12:00:00", priority="EXPRESS"),
]


def test_express_parcels_use_nearest_insertion_inside_the_express_group():
    r = route(EXPRESS_SET)
    assert r.parcel_ids == ("P2", "P3", "P1") and r.unsequenced == ()
    by_id = {p.parcel_id: p for p in EXPRESS_SET}
    hub_sorted = sorted(by_id, key=lambda i: haversine_km(0, 0, by_id[i].dest_lat, by_id[i].dest_lon))
    assert hub_sorted == ["P2", "P1", "P3"] and list(r.parcel_ids) != hub_sorted
    assert tour_km(by_id, r.parcel_ids) < tour_km(by_id, hub_sorted) - 3
    assert_strict(r.stops)


def test_standard_parcels_use_nearest_insertion_inside_the_standard_group():
    # Same geometry as EXPRESS_SET but STANDARD, placed behind one EXPRESS stop E0 (1 km east, first stop 08:04).
    e0 = parcel("E0", 1, 0, we="09:00:00", priority="EXPRESS")
    standards = [Parcel(**{**p.__dict__, "priority": "STANDARD"}) for p in EXPRESS_SET]
    r = route([e0] + standards)
    assert r.parcel_ids == oracle_order([e0] + standards) and r.unsequenced == ()
    assert r.parcel_ids[0] == "E0" and r.stops[1].from_id == "E0"       # the STANDARD block grows from the EXPRESS block's end
    assert_strict(r.stops)


@pytest.mark.parametrize("seed", range(5))
def test_sequencer_matches_an_independent_nearest_insertion_oracle(seed):
    """With slack windows the stop order must equal the oracle's order: nearest insertion inside each priority group,
    ties to the earlier position, STANDARD only after the EXPRESS block."""
    rng = random.Random(seed)
    for _ in range(60):
        n = rng.randint(1, 8)
        ps = [parcel(f"P{i:02d}", rng.randint(-8, 8), rng.randint(-8, 8), ws="08:00:00",
                     we=rng.choice(["16:00:00", "17:00:00", "18:00:00"]),
                     priority=rng.choice(["EXPRESS", "STANDARD"])) for i in range(n)]
        rng.shuffle(ps)
        r = sequence_route(0, 0, "08:00:00", "22:00:00", ps)
        assert r.unsequenced == ()
        assert r.parcel_ids == oracle_order(ps), [(p.parcel_id, p.priority) for p in ps]


# --------------------------------------------------------------------------------------------
# A STANDARD parcel can never go between two EXPRESS stops - even when that is the cheapest spot
# --------------------------------------------------------------------------------------------

BETWEEN_SET = [
    parcel("E1", 10, 0, we="10:00:00", priority="EXPRESS"),
    parcel("E2", 10, 10, we="11:00:00", priority="EXPRESS"),
    parcel("S1", 10, 5, we="12:00:00"),      # lies exactly on the way from E2 to E1
]


def test_standard_is_never_inserted_between_two_express_stops():
    r = route(BETWEEN_SET)
    assert r.parcel_ids == ("E2", "E1", "S1")
    assert_strict(r.stops)
    assert r.stops[2].from_id == "E1"        # STANDARD is reached from the LAST express stop


def test_the_between_position_would_really_have_been_cheaper():
    # Guards the test above: E2 -> S1 -> E1 is a clearly shorter tour than E2 -> E1 -> S1.
    by_id = {p.parcel_id: p for p in BETWEEN_SET}
    assert tour_km(by_id, ("E2", "S1", "E1")) < tour_km(by_id, ("E2", "E1", "S1")) - 3


def test_standard_insertion_never_changes_the_express_block():
    express_only = route(BETWEEN_SET[:2])
    both = route(BETWEEN_SET)
    assert both.stops[:2] == express_only.stops
    assert both.stops[0].parcel_id == "E2" and both.stops[0].priority == "EXPRESS"


# --------------------------------------------------------------------------------------------
# An unsequenced EXPRESS parcel neither blocks other parcels nor lets STANDARD jump the queue
# --------------------------------------------------------------------------------------------

def test_infeasible_express_does_not_block_feasible_express_or_standard():
    bad = parcel("E-bad", east=40, we="10:00:00", priority="EXPRESS")      # 160 min away: can never make 10:00
    ok = parcel("E-ok", east=3, we="12:00:00", priority="EXPRESS")
    s1 = parcel("S1", east=1, we="12:00:00")
    r = route([s1, bad, ok], start="09:00:00")
    assert r.parcel_ids == ("E-ok", "S1")                                   # S1 stays BEHIND the assigned EXPRESS
    assert [(u.parcel_id, u.reason_code) for u in r.unsequenced] == [("E-bad", LATE_FOR_WINDOW)]
    assert_strict(r.stops)


def test_unsequenced_express_is_never_retried_behind_standard_stops():
    bad = parcel("E1", east=7.51, we="12:00:00", priority="EXPRESS")        # first stop 09:30:02.4
    s = [parcel("S1", east=1), parcel("S2", east=2)]
    r = route(s + [bad], start="09:00:00")
    assert "E1" not in r.parcel_ids and r.unsequenced[0].reason_code == EXPRESS_FIRST_STOP_AFTER_0930
    assert set(r.parcel_ids) == {"S1", "S2"} and all(st.priority == "STANDARD" for st in r.stops)


# --------------------------------------------------------------------------------------------
# Randomised guarantee for the sequencer
# --------------------------------------------------------------------------------------------

def _random_parcels(rng, n):
    out = []
    for i in range(n):
        ws = rng.choice([8, 9, 10])
        we = rng.choice([9, 10, 11, 12, 14, 16, 18])
        we = max(we, ws + 1)
        out.append(parcel(f"P{i:02d}", rng.randint(-12, 12), rng.randint(-12, 12), ws=f"{ws:02d}:00:00",
                          we=f"{we:02d}:{rng.choice(['00', '30'])}:00",
                          priority=rng.choice(["EXPRESS", "STANDARD", "STANDARD"]), cod=rng.random() < 0.3))
    return out


def test_strict_priority_holds_for_many_random_mixed_sets_and_nothing_is_dropped():
    rng = random.Random(2026)
    saw_both_kinds = 0
    for _ in range(300):
        ps = _random_parcels(rng, rng.randint(1, 14))
        r = route(ps, start=rng.choice(["08:00:00", "08:30:00", "09:00:00", "10:00:00"]), end="17:00:00")
        assert_strict(r.stops)
        assert sorted(r.parcel_ids + tuple(u.parcel_id for u in r.unsequenced)) == sorted(p.parcel_id for p in ps)
        for s in r.stops:
            assert s.window_start <= s.eta <= s.window_end
        for a, b in zip(r.stops, r.stops[1:]):
            assert b.arrival >= a.departure
        assert r.return_time <= T("17:00:00") + 60 * time_of_day("00:01:00")
        saw_both_kinds += len({s.priority for s in r.stops}) == 2
    assert saw_both_kinds > 30        # the random sets really do mix both priorities in sequenced routes


# --------------------------------------------------------------------------------------------
# Assignment layer: it must pass the sequencer's order through and keep every constraint
# --------------------------------------------------------------------------------------------

def test_assignment_keeps_the_express_block_first_and_does_not_reorder_the_route():
    res = assign_parcels([rider()], [HUB], BETWEEN_SET)
    (a,) = res.assignments
    assert a.parcel_ids == ("E2", "E1", "S1") == a.route.parcel_ids == tuple(s.parcel_id for s in a.route.stops)
    assert_strict(a.route.stops)
    assert res.unassigned == ()


def test_assignment_unassignable_express_does_not_disturb_other_parcels():
    # E-bad can never make its window for ANY rider; E-ok and the STANDARD parcels must still be assigned,
    # all EXPRESS before all STANDARD on every route.
    ps = [parcel("E-bad", east=40, we="10:00:00", priority="EXPRESS"),
          parcel("E-ok", east=3, we="12:00:00", priority="EXPRESS"),
          parcel("S1", east=1), parcel("S2", east=-2, we="13:00:00")]
    res = assign_parcels([rider("RDR-01", start="09:00:00"), rider("RDR-02", start="09:00:00")], [HUB], ps)
    assert res.unassigned_parcel_ids == ("E-bad",) and res.unassigned[0].reason_code == LATE_FOR_WINDOW
    assert sorted(res.assigned_parcel_ids) == ["E-ok", "S1", "S2"]
    for a in res.assignments:
        assert_strict(a.route.stops)
        assert a.parcel_ids == tuple(s.parcel_id for s in a.route.stops) and not a.route.unsequenced


def test_assignment_express_only_feasible_for_the_early_rider_goes_to_that_rider():
    # RDR-01 starts 10:00 -> an EXPRESS first stop would be after 09:30 for her; RDR-02 starts 09:00.
    ps = [parcel("E1", east=3, priority="EXPRESS"), parcel("S1", east=1, we="12:00:00")]
    res = assign_parcels([rider("RDR-01", start="10:00:00"), rider("RDR-02", start="09:00:00")], [HUB], ps)
    assert res.for_rider("RDR-02").parcel_ids[0] == "E1"
    assert res.unassigned == ()
    for a in res.assignments:
        assert_strict(a.route.stops)


def test_assignment_standard_route_is_allowed_when_no_express_can_be_assigned():
    ps = [parcel("E1", east=1, priority="EXPRESS"), parcel("S1", east=1)]
    res = assign_parcels([rider(start="10:00:00")], [HUB], ps)          # every rider starts after 09:30
    assert res.unassigned_parcel_ids == ("E1",) and res.unassigned[0].reason_code == EXPRESS_FIRST_STOP_AFTER_0930
    (a,) = res.assignments
    assert a.parcel_ids == ("S1",) and a.route.stops[0].eta > T("09:30:00")   # the 09:30 rule is not applied to STANDARD


def test_assignment_strict_priority_holds_for_many_random_pools_with_capacity_respected():
    rng = random.Random(99)
    for _ in range(40):
        ps = _random_parcels(rng, rng.randint(5, 30))
        riders = [rider("RDR-01", start="08:00:00"), rider("RDR-02", "EV_BIKE", start="09:00:00"),
                  Rider("RDR-03", "Small", "BIKE", 3, Decimal("25.0"), "HUB-01", time(8), time(17))]
        res = assign_parcels(riders, [HUB], ps)
        ids = list(res.assigned_parcel_ids) + list(res.unassigned_parcel_ids)
        assert sorted(ids) == sorted(p.parcel_id for p in ps)                 # everything accounted for exactly once
        for a in res.assignments:
            assert a.parcel_count <= a.capacity_parcels and a.total_weight_kg <= a.capacity_kg
            assert a.parcel_ids == a.route.parcel_ids and not a.route.unsequenced
            assert_strict(a.route.stops)
