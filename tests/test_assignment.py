"""Tests for ps035.assignment (BR-01 capacity, BR-02 assignment).

Every test uses the real loader records (Hub, Rider, RiderShift, Parcel) and the
real route sequencer. The hub sits at (0, 0), so "east_km"/"north_km" give legs
of the intended length after Haversine rounds them to 2 decimals.
"""
import ast
import math
import random
import socket
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import ps035.assignment as asm
from ps035.assignment import (
    NO_FEASIBLE_ROUTE, NO_RIDER_AVAILABLE, NO_RIDER_CAPACITY, VEHICLE_LIMITS, AssignmentResult,
    RiderAssignment, assign_parcels, vehicle_capacity,
)
from ps035.eta import format_hhmmss, time_of_day
from ps035.loader import Hub, Parcel, Rider, RiderShift, load_dataset
from ps035.route_sequencing import (
    EXPRESS_FIRST_STOP_AFTER_0930, LATE_FOR_WINDOW, RETURN_AFTER_SHIFT_END_PLUS_60, sequence_route,
)

K = 360 / (2 * math.pi * 6371)  # degrees per km on a sphere of radius 6371 km
DAY = date(2026, 10, 12)
T = time_of_day
HUB = Hub("HUB-01", "Test Hub", Decimal("0"), Decimal("0"))


def hhmm(s):
    return time.fromisoformat(s)


def parcel(pid, east=0.0, north=0.0, ws="08:00:00", we="12:00:00", priority="STANDARD", cod=False, kg="1.0"):
    return Parcel(
        parcel_id=pid, pool_date=DAY,
        dest_lat=Decimal(str(round(north * K, 9))), dest_lon=Decimal(str(round(east * K, 9))),
        weight_kg=Decimal(kg), window_start=hhmm(ws), window_end=hhmm(we),
        priority=priority, cod_flag=cod, cod_amount=Decimal("100.00") if cod else Decimal("0.00"))


def rider(rid="RDR-01", vehicle="BIKE", start="08:00:00", end="18:00:00", hub="HUB-01", parcels=None, kg=None):
    default_p, default_kg = {"BIKE": (40, "25.0"), "EV_BIKE": (40, "25.0"), "VAN": (120, "300.0")}[vehicle]
    return Rider(rider_id=rid, rider_name=f"Name {rid}", vehicle_type=vehicle,
                 capacity_parcels=parcels or default_p, capacity_kg=Decimal(kg or default_kg),
                 home_hub=hub, default_shift_start=hhmm(start), default_shift_end=hhmm(end))


def chain(n, kg_each, prefix="PCL-"):
    """n parcels at the same spot 1 km north of the hub with TIGHT windows.

    Parcel i's window ends exactly at its own ETA (06:04 + 3 min * (i-1)), so the
    only feasible position for the next parcel is the end of the route. That keeps
    the sequencer fast even for 120 parcels and makes the processing order i = 1..n.
    """
    first = timedelta(hours=6, minutes=4)
    out = []
    for i in range(1, n + 1):
        end = first + timedelta(minutes=3 * (i - 1))
        h, rem = divmod(int(end.total_seconds()), 3600)
        out.append(parcel(f"{prefix}{i:05d}", north=1, ws="06:00:00", we=f"{h:02d}:{rem // 60:02d}:{rem % 60:02d}",
                          kg=kg_each))
    return out


WIDE = dict(start="06:00:00", end="20:00:00")


def run(riders, parcels, hubs=(HUB,), **kw):
    return assign_parcels(riders, list(hubs), parcels, **kw)


def assigned(result, rid):
    return result.for_rider(rid).parcel_ids


def assert_accounted_exactly_once(result, parcels):
    ids = list(result.assigned_parcel_ids) + list(result.unassigned_parcel_ids)
    assert sorted(ids) == sorted(p.parcel_id for p in parcels)
    assert len(ids) == len(set(ids))


def assert_strict_priority(a):
    kinds = [s.priority for s in a.route.stops]
    assert kinds == sorted(kinds, key=lambda k: k != "EXPRESS")


# ---- BR-01 capacity: BIKE / EV_BIKE ----------------------------------------------------------------

def test_vehicle_limits_table():
    assert vehicle_capacity("BIKE") == (40, Decimal("25.0"))
    assert vehicle_capacity("EV_BIKE") == (40, Decimal("25.0"))
    assert vehicle_capacity("VAN") == (120, Decimal("300.0"))
    assert set(VEHICLE_LIMITS) == {"BIKE", "EV_BIKE", "VAN"}


def test_bike_accepts_exactly_40_parcels_within_25_kg():
    ps = chain(40, "0.625")  # 40 * 0.625 = 25.0 kg exactly
    r = run([rider(**WIDE)], ps)
    a = r.for_rider("RDR-01")
    assert a.parcel_count == 40 and a.total_weight_kg == Decimal("25.0")
    assert (a.remaining_parcels, a.remaining_kg) == (0, Decimal("0.0"))
    assert r.unassigned == () and r.status == "COMPLETE"


def test_bike_rejects_a_41st_parcel():
    ps = chain(41, "0.5")  # 20.5 kg: only the COUNT limit is hit
    r = run([rider(**WIDE)], ps)
    assert r.for_rider("RDR-01").parcel_count == 40
    (u,) = r.unassigned
    assert u.parcel_id == "PCL-00041" and u.reason_code == NO_RIDER_CAPACITY
    assert r.status == "PARTIAL"


def test_bike_rejects_total_weight_above_25_kg():
    ps = [parcel("PCL-00001", east=1, we="09:00:00", kg="12.5"), parcel("PCL-00002", east=2, we="09:30:00", kg="12.5"),
          parcel("PCL-00003", east=3, we="10:00:00", kg="0.1")]
    r = run([rider()], ps)
    assert set(assigned(r, "RDR-01")) == {"PCL-00001", "PCL-00002"}  # exactly 25.0 kg accepted
    (u,) = r.unassigned
    assert u.parcel_id == "PCL-00003" and u.reason_code == NO_RIDER_CAPACITY and "exceeds 25.0 kg" in u.detail


@pytest.mark.parametrize("vehicle", ["BIKE", "EV_BIKE"])
def test_bike_and_ev_bike_share_the_same_limits(vehicle):
    ok = run([rider(vehicle=vehicle, **WIDE)], chain(40, "0.625"))
    assert ok.for_rider("RDR-01").parcel_count == 40 and ok.unassigned == ()
    over_count = run([rider(vehicle=vehicle, **WIDE)], chain(41, "0.5"))
    assert over_count.unassigned_parcel_ids == ("PCL-00041",)
    over_kg = run([rider(vehicle=vehicle)], [parcel("A", east=1, kg="25.0"), parcel("B", east=1, kg="0.1", we="13:00:00")])
    assert over_kg.unassigned_parcel_ids == ("B",)


def test_exact_weight_boundary_is_accepted_and_a_tiny_excess_rejected():
    assert run([rider()], [parcel("A", east=1, kg="25.0")]).unassigned == ()
    for tiny in ("25.1", "25.01", "25.0000001", "25.00000000000000000001"):  # beyond float precision too
        r = run([rider()], [parcel("A", east=1, kg=tiny)])
        assert r.unassigned_parcel_ids == ("A",), tiny
        assert r.unassigned[0].reason_code == NO_RIDER_CAPACITY


def test_decimal_weights_are_summed_exactly():
    # 8.3 + 8.3 + 8.3 + 0.1 is exactly 25.0 in Decimal (float arithmetic would give 25.000000000000004).
    ps = [parcel(f"P{i}", east=i, we=f"{9 + i:02d}:00:00", kg="8.3") for i in (1, 2, 3)]
    ps.append(parcel("P4", east=4, we="14:00:00", kg="0.1"))
    r = run([rider()], ps)
    assert r.unassigned == () and r.for_rider("RDR-01").total_weight_kg == Decimal("25.0")
    assert isinstance(r.for_rider("RDR-01").total_weight_kg, Decimal)


def test_a_smaller_capacity_on_the_rider_record_is_respected():
    # Vehicle limit says 40, the record says 1: the smaller value wins, so no limit is ever exceeded.
    ps = [parcel("A", east=1, we="09:00:00"), parcel("B", east=2, we="10:00:00")]
    r = run([rider(parcels=1)], ps)
    assert assigned(r, "RDR-01") == ("A",) and r.unassigned[0].reason_code == NO_RIDER_CAPACITY
    r2 = run([rider(kg="1.5")], [parcel("A", east=1, kg="1.0"), parcel("B", east=2, kg="1.0", we="13:00:00")])
    assert r2.unassigned_parcel_ids == ("B",)


# ---- BR-01 capacity: VAN ---------------------------------------------------------------------------

def test_van_accepts_exactly_120_parcels_within_300_kg():
    r = run([rider(vehicle="VAN", **WIDE)], chain(120, "2.5"))  # 120 * 2.5 = 300.0 kg exactly
    a = r.for_rider("RDR-01")
    assert a.parcel_count == 120 and a.total_weight_kg == Decimal("300.0")
    assert (a.remaining_parcels, a.remaining_kg) == (0, Decimal("0.0")) and r.unassigned == ()


def test_van_rejects_a_121st_parcel():
    r = run([rider(vehicle="VAN", **WIDE)], chain(121, "1.0"))
    assert r.for_rider("RDR-01").parcel_count == 120
    assert r.unassigned_parcel_ids == ("PCL-00121",) and r.unassigned[0].reason_code == NO_RIDER_CAPACITY


def test_van_rejects_weight_above_300_kg_and_accepts_exactly_300():
    ps = [parcel(f"P{i:02d}", east=i, we=f"{8 + i // 2:02d}:{(i % 2) * 30:02d}:00", kg="15.0") for i in range(1, 21)]
    ps.append(parcel("P99", east=1, we="23:00:00", kg="0.1"))
    r = run([rider(vehicle="VAN", start="06:00:00", end="22:00:00")], ps)  # 20 * 15.0 = 300.0 kg exactly
    a = r.for_rider("RDR-01")
    assert a.total_weight_kg == Decimal("300.0") and a.parcel_count == 20
    assert r.unassigned_parcel_ids == ("P99",) and r.unassigned[0].reason_code == NO_RIDER_CAPACITY


# ---- BR-02 priority ----------------------------------------------------------------------------------

def test_express_parcels_are_processed_before_standard():
    # One slot only (record capacity 1): the EXPRESS parcel gets it although the STANDARD one has an earlier window_end.
    standard = parcel("S1", east=1, we="09:00:00")
    express = parcel("E1", east=2, we="12:00:00", priority="EXPRESS")
    r = run([rider(parcels=1, start="08:00:00")], [standard, express])
    assert assigned(r, "RDR-01") == ("E1",) and r.unassigned_parcel_ids == ("S1",)


def test_inside_a_tier_earlier_window_end_then_parcel_id_goes_first():
    r = run([rider(parcels=1)], [parcel("S2", east=1, we="10:00:00"), parcel("S1", east=2, we="11:00:00")])
    assert assigned(r, "RDR-01") == ("S2",)  # earlier window_end beats the lower id
    r = run([rider(parcels=1)], [parcel("S2", east=1, we="10:00:00"), parcel("S1", east=2, we="10:00:00")])
    assert assigned(r, "RDR-01") == ("S1",)  # equal window_end: lower parcel_id


def test_every_rider_route_has_all_express_before_all_standard():
    rng = random.Random(11)
    ps = [parcel(f"PCL-{i:05d}", rng.uniform(-8, 8), rng.uniform(-8, 8), ws="08:00:00", we=f"{rng.choice([11, 13, 16]):02d}:00:00",
                 priority=rng.choice(["EXPRESS", "STANDARD"]), kg="0.5") for i in range(1, 25)]
    r = run([rider("RDR-01", start="08:00:00"), rider("RDR-02", start="08:00:00"), rider("RDR-03", "VAN", start="08:00:00")], ps)
    assert_accounted_exactly_once(r, ps)
    for a in r.assignments:
        assert_strict_priority(a)
        assert not a.route.unsequenced and set(a.parcel_ids) == {s.parcel_id for s in a.route.stops}


def test_assignment_reuses_the_existing_route_sequencer(monkeypatch):
    calls = []
    real = asm.sequence_route

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(asm, "sequence_route", spy)
    ps = [parcel("S1", east=2, we="10:00:00"), parcel("E1", east=3, we="10:00:00", priority="EXPRESS")]
    r = run([rider(start="08:00:00")], ps)
    assert len(calls) >= 3  # empty-route baseline + one trial per parcel
    a = r.for_rider("RDR-01")
    assigned_parcels = [p for p in ps if p.parcel_id in a.parcel_ids]
    assert a.route == sequence_route(HUB.lat, HUB.lon, a.shift_start, a.shift_end, assigned_parcels, hub_id="HUB-01")
    assert a.parcel_ids[0] == "E1"  # strict priority comes from the sequencer


# ---- BR-02 route feasibility ------------------------------------------------------------------------------

def test_a_parcel_that_would_break_an_assigned_parcel_is_not_assigned():
    # A is reached at exactly 09:24 (window_end 09:24). B (3 km) before A would delay A to 09:27;
    # B after A arrives 09:39 > its window_end 09:30. So B cannot join this rider.
    a = parcel("A", east=6, we="09:24:00")
    b = parcel("B", east=3, we="09:30:00")
    r = run([rider(start="09:00:00", end="17:00:00")], [a, b])
    ra = r.for_rider("RDR-01")
    assert ra.parcel_ids == ("A",) and ra.route.stops[0].eta == T("09:24:00") and not ra.route.unsequenced
    (u,) = r.unassigned
    assert u.parcel_id == "B" and u.reason_code == LATE_FOR_WINDOW


def test_delivery_window_infeasibility_has_an_explicit_reason():
    r = run([rider(start="09:00:00")], [parcel("A", east=15.01, we="10:00:00")])  # 60.04 min away
    (u,) = r.unassigned
    assert u.reason_code == LATE_FOR_WINDOW and "RDR-01" in u.detail and "10:00:00" in u.detail
    assert r.status == "NONE_ASSIGNED"


def test_express_first_stop_after_0930_is_not_accepted():
    r = run([rider(start="09:00:00")], [parcel("E1", east=7.51, priority="EXPRESS")])
    assert r.unassigned[0].reason_code == EXPRESS_FIRST_STOP_AFTER_0930
    assert assigned(r, "RDR-01") == ()


def test_express_first_stop_exactly_0930_is_accepted():
    r = run([rider(start="09:00:00")], [parcel("E1", east=7.5, priority="EXPRESS")])
    a = r.for_rider("RDR-01")
    assert a.parcel_ids == ("E1",) and format_hhmmss(a.route.stops[0].eta) == "09:30:00" and r.unassigned == ()


def test_shift_return_exactly_shift_end_plus_60_is_accepted():
    # 14 km each way = 112 min + 8 min COD service = back at exactly 10:00:00 = 09:00:00 + 60 min.
    r = run([rider(start="08:00:00", end="09:00:00")], [parcel("A", east=14, cod=True)])
    a = r.for_rider("RDR-01")
    assert a.parcel_ids == ("A",) and a.route.return_time == T("10:00:00") and a.route.uses_overtime


def test_shift_return_one_microsecond_later_is_rejected():
    r = assign_parcels([Rider("RDR-01", "x", "BIKE", 40, Decimal("25.0"), "HUB-01", hhmm("08:00:00"), time(8, 59, 59, 999_999))],
                       [HUB], [parcel("A", east=14, cod=True)])
    assert r.unassigned[0].reason_code == RETURN_AFTER_SHIFT_END_PLUS_60 and assigned(r, "RDR-01") == ()


# ---- BR-02 choosing a rider ------------------------------------------------------------------------------

def test_parcel_goes_to_another_rider_when_the_first_is_at_capacity():
    ps = [parcel("A", east=1, we="09:00:00"), parcel("B", east=1, we="10:00:00")]
    r = run([rider("RDR-01", parcels=1), rider("RDR-02")], ps)
    assert assigned(r, "RDR-01") == ("A",) and assigned(r, "RDR-02") == ("B",) and r.unassigned == ()


def test_parcel_goes_to_another_rider_when_the_first_is_infeasible():
    # RDR-01's shift ends at 09:00 (back by 10:00); the parcel is 14.01 km away (COD): too late for RDR-01 only.
    ps = [parcel("A", east=14.01, cod=True)]
    r = run([rider("RDR-01", start="08:00:00", end="09:00:00"), rider("RDR-02", start="08:00:00", end="18:00:00")], ps)
    assert assigned(r, "RDR-01") == () and assigned(r, "RDR-02") == ("A",)


def test_choose_the_rider_with_the_lowest_added_route_distance():
    hub2 = Hub("HUB-02", "East Hub", Decimal("0"), Decimal(str(round(20 * K, 9))))  # 20 km east of HUB-01
    riders = [rider("RDR-01", hub="HUB-01"), rider("RDR-02", hub="HUB-02")]
    r = run(riders, [parcel("A", east=19, we="12:00:00")], hubs=(HUB, hub2))
    # Added km: RDR-01 = 38.0, RDR-02 = 2.0 -> the higher rider_id wins because it is cheaper.
    assert assigned(r, "RDR-02") == ("A",) and assigned(r, "RDR-01") == ()
    assert r.for_rider("RDR-02").hub_id == "HUB-02" and r.for_rider("RDR-02").route.total_km == 2.0


def test_added_distance_not_total_distance_decides():
    # RDR-01 already carries a 20 km round trip; the new parcel is right next to it (added ~1 km),
    # whereas the empty RDR-02 would add 21 km. RDR-01 wins even though its TOTAL becomes larger.
    ps = [parcel("A", east=10, we="10:00:00"), parcel("B", east=10.5, we="11:00:00")]
    r = run([rider("RDR-01", start="08:00:00"), rider("RDR-02", start="08:00:00")], ps)
    assert set(assigned(r, "RDR-01")) == {"A", "B"} and assigned(r, "RDR-02") == ()


def test_equal_added_distance_is_broken_by_rider_id_whatever_the_input_order():
    for riders in ([rider("RDR-02"), rider("RDR-01")], [rider("RDR-01"), rider("RDR-02")]):
        r = run(riders, [parcel("A", east=3)])
        assert assigned(r, "RDR-01") == ("A",) and assigned(r, "RDR-02") == ()
    assert [a.rider_id for a in r.assignments] == ["RDR-01", "RDR-02"]


def test_riders_with_different_shifts_and_hubs_are_routed_from_their_own_hub_and_shift():
    hub2 = Hub("HUB-02", "North Hub", Decimal(str(round(30 * K, 9))), Decimal("0"))
    morning = rider("RDR-01", start="08:00:00", end="12:00:00", hub="HUB-01")
    afternoon = rider("RDR-02", start="14:00:00", end="18:00:00", hub="HUB-02")
    ps = [parcel("AM", east=3, ws="08:00:00", we="10:00:00"), parcel("PM", north=31, ws="14:00:00", we="16:00:00")]
    r = run([afternoon, morning], ps, hubs=(hub2, HUB))
    assert assigned(r, "RDR-01") == ("AM",) and assigned(r, "RDR-02") == ("PM",)
    assert r.for_rider("RDR-01").route.hub_id == "HUB-01" and r.for_rider("RDR-02").route.hub_id == "HUB-02"
    assert r.for_rider("RDR-02").route.stops[0].leg_km == 1.0  # measured from HUB-02, not HUB-01
    assert r.for_rider("RDR-02").shift_start == hhmm("14:00:00")


def test_roster_uses_the_shift_for_the_plan_date_and_skips_riders_without_one():
    riders = [rider("RDR-01", start="08:00:00", end="17:00:00"), rider("RDR-02"), rider("RDR-03")]
    shifts = [RiderShift("RDR-01", DAY, hhmm("13:00:00"), hhmm("17:00:00")),
              RiderShift("RDR-02", date(2026, 10, 13), hhmm("08:00:00"), hhmm("18:00:00")),   # other day only
              RiderShift("RDR-03", DAY, hhmm("08:00:00"), hhmm("18:00:00"))]
    ps = [parcel("A", east=2, ws="08:00:00", we="10:00:00")]  # impossible for RDR-01 (shift starts 13:00)
    r = run(riders, ps, shifts=shifts, plan_date=DAY)
    assert r.off_roster_rider_ids == ("RDR-02",)
    assert [a.rider_id for a in r.assignments] == ["RDR-01", "RDR-03"]
    assert r.for_rider("RDR-01").shift_start == hhmm("13:00:00") and assigned(r, "RDR-03") == ("A",)


# ---- determinism and accounting --------------------------------------------------------------------------

def mixed_scenario():
    rng = random.Random(5)
    ps = [parcel(f"PCL-{i:05d}", rng.uniform(-12, 12), rng.uniform(-12, 12), ws=f"{rng.choice([8, 9, 10]):02d}:00:00",
                 we=f"{rng.choice([10, 12, 14]):02d}:00:00", priority=rng.choice(["EXPRESS", "STANDARD"]),
                 cod=rng.random() < 0.3, kg=rng.choice(["0.5", "1.0", "2.5"])) for i in range(1, 31)]
    riders = [rider("RDR-01", start="08:00:00", end="15:00:00"), rider("RDR-02", start="09:00:00", end="16:00:00"),
              rider("RDR-03", "EV_BIKE", start="08:30:00", end="14:00:00")]
    return riders, ps


def test_result_does_not_depend_on_input_order():
    riders, ps = mixed_scenario()
    expected = run(riders, ps)
    rng = random.Random(1)
    for _ in range(10):
        r2, p2 = riders[:], ps[:]
        rng.shuffle(r2)
        rng.shuffle(p2)
        assert run(r2, p2) == expected


def test_every_parcel_is_assigned_or_unassigned_exactly_once_and_to_one_rider_only():
    riders, ps = mixed_scenario()
    r = run(riders, ps)
    assert r.unassigned, "scenario should leave some parcels unassigned"
    assert_accounted_exactly_once(r, ps)
    owners = [pid for a in r.assignments for pid in a.parcel_ids]
    assert len(owners) == len(set(owners))
    for a in r.assignments:
        assert a.parcel_count == len(a.parcel_ids) == len(a.route.stops) and not a.route.unsequenced
        assert a.total_weight_kg == sum((next(p.weight_kg for p in ps if p.parcel_id == x) for x in a.parcel_ids), Decimal(0))
        assert a.remaining_parcels >= 0 and a.remaining_kg >= 0


def test_unassignable_parcel_keeps_a_clear_reason():
    r = run([rider("RDR-01"), rider("RDR-02")], [parcel("FAR", east=500)])
    (u,) = r.unassigned
    assert u.parcel_id == "FAR" and u.reason_code == LATE_FOR_WINDOW
    assert "RDR-01" in u.detail and "RDR-02" in u.detail


def test_different_failure_reasons_per_rider_give_no_feasible_route():
    # B is 20 km away (window 08:00-09:30, no EXPRESS rule). RDR-01 (shift 08:00-08:30) reaches it in time
    # but cannot get back by 09:30; RDR-03 (shift 09:00-18:00) would arrive after the window closes.
    ps = [parcel("B", east=20, we="09:30:00")]
    r = run([rider("RDR-01", start="08:00:00", end="08:30:00"), rider("RDR-03", start="09:00:00", end="18:00:00")], ps)
    (u,) = r.unassigned
    assert u.parcel_id == "B" and u.reason_code == NO_FEASIBLE_ROUTE
    assert RETURN_AFTER_SHIFT_END_PLUS_60 in u.detail and LATE_FOR_WINDOW in u.detail


def test_capacity_refusals_do_not_hide_the_route_reason():
    # RDR-02 is full, RDR-01 is too late: the single ROUTE reason is reported, the capacity refusal stays in the detail.
    ps = [parcel("A", east=1, we="09:00:00"), parcel("B", east=15.01, we="10:00:00")]
    r = run([rider("RDR-01", start="09:30:00"), rider("RDR-02", parcels=1, start="08:00:00")], ps)
    assert assigned(r, "RDR-02") == ("A",) and r.unassigned[0].parcel_id == "B"
    assert r.unassigned[0].reason_code == LATE_FOR_WINDOW and "parcel limit 1 reached" in r.unassigned[0].detail


def test_all_riders_full_gives_no_rider_capacity():
    r = run([rider("RDR-01", parcels=1), rider("RDR-02", parcels=1)],
            [parcel("A", east=1, we="09:00:00"), parcel("B", east=2, we="10:00:00"), parcel("C", east=3, we="11:00:00")])
    assert r.unassigned[0].parcel_id == "C" and r.unassigned[0].reason_code == NO_RIDER_CAPACITY


# ---- empty inputs ---------------------------------------------------------------------------------------

def test_empty_parcel_list_gives_empty_routes_for_every_rider():
    r = run([rider("RDR-02"), rider("RDR-01")], [])
    assert isinstance(r, AssignmentResult) and r.status == "COMPLETE" and r.unassigned == ()
    assert [a.rider_id for a in r.assignments] == ["RDR-01", "RDR-02"]
    for a in r.assignments:
        assert isinstance(a, RiderAssignment) and a.parcel_ids == () and a.route.stops == ()
        assert (a.parcel_count, a.total_weight_kg, a.remaining_parcels, a.remaining_kg) == (0, Decimal(0), 40, Decimal("25.0"))


def test_empty_rider_list_leaves_every_parcel_unassigned_with_a_reason():
    ps = [parcel("A", east=1), parcel("B", east=2)]
    r = run([], ps)
    assert r.assignments == () and r.status == "NONE_ASSIGNED"
    assert {u.reason_code for u in r.unassigned} == {NO_RIDER_AVAILABLE}
    assert_accounted_exactly_once(r, ps)


def test_empty_riders_and_parcels():
    r = run([], [])
    assert r.assignments == () and r.unassigned == () and r.status == "COMPLETE"


def test_nobody_on_the_roster_means_no_rider_available():
    r = run([rider("RDR-01")], [parcel("A", east=1)], shifts=[], plan_date=DAY)
    assert r.off_roster_rider_ids == ("RDR-01",) and r.unassigned[0].reason_code == NO_RIDER_AVAILABLE


# ---- invalid input ------------------------------------------------------------------------------------------

def test_unknown_vehicle_type_rejected():
    bad = Rider("RDR-01", "x", "TRUCK", 40, Decimal("25.0"), "HUB-01", hhmm("08:00:00"), hhmm("18:00:00"))
    with pytest.raises(ValueError, match="vehicle_type"):
        run([bad], [])
    with pytest.raises(ValueError, match="vehicle_type"):
        vehicle_capacity("TRUCK")


def test_invalid_weights_rejected():
    for bad, exc in ((Decimal("-0.1"), ValueError), (Decimal("NaN"), ValueError), (Decimal("Infinity"), ValueError),
                     (float("nan"), ValueError), ("5", TypeError), (None, TypeError), (True, TypeError)):
        p = parcel("A", east=1)
        p = Parcel(**{**p.__dict__, "weight_kg": bad})
        with pytest.raises(exc, match="weight_kg"):
            run([rider()], [p])


def test_invalid_rider_capacity_fields_rejected():
    for bad in (0, -5, True):
        with pytest.raises(ValueError, match="capacity_parcels"):
            run([Rider("RDR-01", "x", "BIKE", bad, Decimal("25.0"), "HUB-01", hhmm("08:00:00"), hhmm("18:00:00"))], [])
    with pytest.raises(ValueError, match="capacity_kg"):
        run([Rider("RDR-01", "x", "BIKE", 40, Decimal("-1"), "HUB-01", hhmm("08:00:00"), hhmm("18:00:00"))], [])


def test_invalid_coordinates_rejected_with_the_parcel_id():
    p = Parcel(**{**parcel("PCL-00007", east=1).__dict__, "dest_lat": Decimal("95")})
    with pytest.raises(ValueError, match="PCL-00007"):
        run([rider()], [p])
    with pytest.raises(ValueError, match="PCL-00007"):
        run([], [p])  # validated even when there is nobody to assign to


def test_invalid_time_window_rejected():
    with pytest.raises(ValueError, match="window_end"):
        run([rider()], [parcel("A", east=1, ws="12:00:00", we="09:00:00")])


def test_invalid_priority_and_cod_flag_rejected():
    with pytest.raises(ValueError, match="priority"):
        run([rider()], [parcel("A", east=1, priority="URGENT")])
    with pytest.raises(TypeError, match="cod_flag"):
        run([rider()], [Parcel(**{**parcel("A", east=1).__dict__, "cod_flag": "true"})])


def test_duplicate_parcel_and_rider_ids_rejected():
    with pytest.raises(ValueError, match="duplicate parcel_id 'A'"):
        run([rider()], [parcel("A", east=1), parcel("A", east=2)])
    with pytest.raises(ValueError, match="duplicate rider_id 'RDR-01'"):
        run([rider("RDR-01"), rider("RDR-01")], [])


def test_unknown_or_duplicate_hub_rejected():
    with pytest.raises(ValueError, match="home_hub"):
        run([rider(hub="HUB-09")], [])
    with pytest.raises(ValueError, match="duplicate hub_id"):
        run([rider()], [], hubs=(HUB, HUB))


def test_shift_before_start_and_roster_argument_errors():
    with pytest.raises(ValueError, match="shift_end"):
        run([rider(start="10:00:00", end="09:00:00")], [])
    with pytest.raises(ValueError, match="plan_date"):
        run([rider()], [], shifts=[])
    with pytest.raises(ValueError, match="plan_date"):
        run([rider()], [], plan_date=DAY)
    with pytest.raises(ValueError, match="duplicate shift"):
        run([rider()], [], shifts=[RiderShift("RDR-01", DAY, hhmm("08:00:00"), hhmm("17:00:00"))] * 2, plan_date=DAY)


def test_wrong_input_types_rejected():
    with pytest.raises(TypeError):
        run([{"rider_id": "RDR-01"}], [])
    with pytest.raises(TypeError):
        run([rider()], [{"parcel_id": "A"}])
    with pytest.raises(TypeError):
        run("RDR-01", [])
    with pytest.raises(TypeError):
        assign_parcels([rider()], [HUB], "PCL-00001")


def test_any_iterable_is_accepted():
    r = assign_parcels(iter([rider()]), iter([HUB]), iter([parcel("A", east=1)]))
    assert assigned(r, "RDR-01") == ("A",)


# ---- local only -----------------------------------------------------------------------------------------------

def test_module_imports_only_standard_library_and_project_modules():
    tree = ast.parse(Path(asm.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "dataclasses", "datetime", "decimal", "typing", "ps035"}


def test_works_with_networking_disabled(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    assert assigned(run([rider()], [parcel("A", east=1)]), "RDR-01") == ("A",)


# ---- official sample data (read-only) -------------------------------------------------------------------------

DATA = Path(__file__).resolve().parent.parent / "data"
needs_data = pytest.mark.skipif(not (DATA / "parcels.csv").is_file(), reason="sample data not present")


def plan_day(ds, day):
    pool = [p for p in ds.parcels if p.pool_date == day]
    return pool, assign_parcels(ds.riders, ds.hubs, pool, ds.shifts, day)


@needs_data
def test_official_ec01_a_rider_at_25_kg_cannot_take_the_0_1_kg_parcel():
    ds = load_dataset(DATA)
    pool, r = plan_day(ds, date(2026, 10, 15))
    rdr06 = r.for_rider("RDR-06")
    first_eight = tuple(f"PCL-{n:05d}" for n in range(751, 759))
    assert set(first_eight) <= set(rdr06.parcel_ids)
    assert rdr06.total_weight_kg == Decimal("25.0") and rdr06.remaining_kg == Decimal("0.0")
    assert "PCL-00759" in r.unassigned_parcel_ids  # the 0.1 kg parcel (window to 09:31) cannot be added
    assert_accounted_exactly_once(r, pool)


@needs_data
def test_official_ec02_express_parcel_goes_to_the_only_rider_who_reaches_it_by_0930():
    ds = load_dataset(DATA)
    day = date(2026, 10, 13)
    express = [p for p in ds.parcels if p.parcel_id == "PCL-00251"]  # the day's only EXPRESS parcel
    r = assign_parcels(ds.riders, ds.hubs, express, ds.shifts, day)
    owner = next(a for a in r.assignments if "PCL-00251" in a.parcel_ids)
    assert owner.rider_id == "RDR-08" and r.unassigned == ()
    assert owner.route.stops[0].parcel_id == "PCL-00251" and format_hhmmss(owner.route.stops[0].eta) == "09:30:00"


@needs_data
def test_official_full_day_accounts_for_every_parcel_and_keeps_strict_priority():
    ds = load_dataset(DATA)
    pool, r = plan_day(ds, date(2026, 10, 12))
    assert len(pool) == 250
    assert_accounted_exactly_once(r, pool)
    for a in r.assignments:
        assert_strict_priority(a)
        assert a.parcel_count <= a.capacity_parcels and a.total_weight_kg <= a.capacity_kg
        assert not a.route.unsequenced
