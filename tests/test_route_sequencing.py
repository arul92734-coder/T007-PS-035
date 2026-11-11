import ast
import math
import random
import socket
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import ps035.route_sequencing as rs_module
from ps035.distance import haversine_km
from ps035.eta import format_hhmmss, time_of_day
from ps035.loader import Parcel, load_dataset
from ps035.route_sequencing import (
    EXPRESS_FIRST_STOP_AFTER_0930, LATE_FOR_WINDOW, RETURN_AFTER_SHIFT_END_PLUS_60,
    RouteResult, sequence_route,
)

MIN = timedelta(minutes=1)
T = time_of_day
# Degrees of longitude (or latitude) that equal 1 km on a sphere of radius 6371 km.
# Hub is at (0, 0), so "east_km" / "north_km" give legs of the intended length
# after Haversine rounds to 2 decimals.
K = 360 / (2 * math.pi * 6371)


def parcel(pid, east=0.0, north=0.0, ws="08:00:00", we="12:00:00", priority="STANDARD", cod=False):
    return Parcel(
        parcel_id=pid, pool_date=date(2026, 10, 12),
        dest_lat=Decimal(str(round(north * K, 9))), dest_lon=Decimal(str(round(east * K, 9))),
        weight_kg=Decimal("1.0"), window_start=time.fromisoformat(ws), window_end=time.fromisoformat(we),
        priority=priority, cod_flag=cod, cod_amount=Decimal("100.00") if cod else Decimal("0.00"))


def route(parcels, start="09:00:00", end="17:00:00"):
    return sequence_route(0, 0, start, end, parcels)


# ---- 1. basic one-parcel route ------------------------------------------------

def test_one_parcel_with_return_leg():
    r = route([parcel("P1", east=3, ws="09:00:00", we="12:00:00")])
    (s,) = r.stops
    assert (s.seq, s.parcel_id, s.from_id, s.leg_km) == (1, "P1", "HUB", 3.0)
    assert s.arrival == T("09:12:00") and s.wait == timedelta(0) and s.eta == T("09:12:00")
    assert s.service == 3 * MIN and s.departure == T("09:15:00")
    assert (r.return_from_id, r.return_leg_km, r.return_time) == ("P1", 3.0, T("09:27:00"))
    assert r.total_km == 6.0
    assert r.status == "COMPLETE" and r.unsequenced == () and r.uses_overtime is False
    assert isinstance(r, RouteResult) and format_hhmmss(s.eta) == "09:12:00"


# ---- 2-3. real insertion logic --------------------------------------------------

# Parcels (km east, km north of the hub), processed in this order (earliest window_end first):
#   P1 (7, 4)   P2 (-2, -5)   P3 (7, -8)
# Insertion steps: [P1]; P2 ties between both ends -> earlier position -> [P2, P1];
# P3 is cheapest between P2 and P1 -> [P2, P3, P1]  (34.94 km).
# Simply sorting by distance from the hub would give [P2, P1, P3]  (40.75 km): longer.
INSERTION_SET = [
    parcel("P1", 7, 4, we="10:00:00"),
    parcel("P2", -2, -5, we="11:00:00"),
    parcel("P3", 7, -8, we="12:00:00"),
]


def test_nearest_insertion_chooses_the_cheapest_position():
    r = route(INSERTION_SET, start="08:00:00", end="18:00:00")
    assert r.parcel_ids == ("P2", "P3", "P1")
    assert r.total_km == pytest.approx(34.94, abs=0.011)


def test_result_is_not_just_a_sort_by_distance_from_hub():
    r = route(INSERTION_SET, start="08:00:00", end="18:00:00")
    hub_dist = {p.parcel_id: haversine_km(0, 0, p.dest_lat, p.dest_lon) for p in INSERTION_SET}
    sorted_by_hub = sorted(hub_dist, key=hub_dist.get)
    assert sorted_by_hub == ["P2", "P1", "P3"] and list(r.parcel_ids) != sorted_by_hub

    def length(order):
        pts = {p.parcel_id: (p.dest_lat, p.dest_lon) for p in INSERTION_SET}
        path = [(0, 0)] + [pts[i] for i in order] + [(0, 0)]
        return sum(haversine_km(*a, *b) for a, b in zip(path, path[1:]))

    assert length(r.parcel_ids) < length(sorted_by_hub) - 3


# ---- 4-5. priority and window_end ordering ---------------------------------------
# Shift 09:00-09:00 means the rider must be back by 10:00. Parcels sit on OPPOSITE
# sides of the hub, so only ONE of them fits. The one processed first wins.

def test_express_beats_standard_even_when_standard_is_closer_and_more_urgent():
    express = parcel("E1", east=6, we="12:00:00", priority="EXPRESS")
    standard = parcel("S1", east=-5, we="10:00:00")  # nearer, earlier window_end
    r = route([standard, express], start="09:00:00", end="09:00:00")
    assert r.parcel_ids == ("E1",)
    assert [u.parcel_id for u in r.unsequenced] == ["S1"]
    # Strict priority: S1's only allowed slot is AFTER E1 (arrives 10:11, window_end 10:00).
    assert r.unsequenced[0].reason_code == LATE_FOR_WINDOW
    assert r.status == "PARTIAL"


def test_earlier_window_end_wins_within_the_same_priority():
    urgent = parcel("S2", east=6, we="10:00:00")
    relaxed = parcel("S1", east=-5, we="11:00:00")  # lower id and nearer, but later window_end
    r = route([relaxed, urgent], start="09:00:00", end="09:00:00")
    assert r.parcel_ids == ("S2",)
    assert [u.parcel_id for u in r.unsequenced] == ["S1"]


# ---- 6. deterministic tie-breaks ---------------------------------------------------

def test_equal_priority_and_window_end_lowest_parcel_id_goes_first():
    a = parcel("PCL-00001", east=-5, we="11:00:00")
    b = parcel("PCL-00002", east=5, we="11:00:00")
    r = route([b, a], start="09:00:00", end="09:00:00")  # only one fits
    assert r.parcel_ids == ("PCL-00001",)


def test_result_does_not_depend_on_input_order():
    ps = INSERTION_SET + [parcel("P4", 1, 1, we="12:00:00")]
    forward = route(ps, start="08:00:00", end="18:00:00")
    backward = route(list(reversed(ps)), start="08:00:00", end="18:00:00")
    assert forward == backward


def test_equal_insertion_cost_goes_to_the_earlier_position():
    # B sits exactly where A sits: both positions add the same distance (0.0).
    a = parcel("A", east=5, we="10:00:00")
    b = parcel("B", east=5, we="11:00:00")
    assert route([a, b]).parcel_ids == ("B", "A")


# ---- strict priority (BR-02): [all EXPRESS] then [all STANDARD] ---------------------------------

def assert_strict_priority(r):
    """No STANDARD stop before any EXPRESS stop, and no EXPRESS stop after a STANDARD stop."""
    kinds = [s.priority for s in r.stops]
    assert kinds == sorted(kinds, key=lambda k: k != "EXPRESS"), kinds
    if "EXPRESS" in kinds:
        assert kinds[0] == "EXPRESS"


# Hub (0,0). E2 (10 km E, 10 km N) and E1 (10 km E). A STANDARD parcel S1 at (2 E, 3 N) is
# CHEAPEST at position 0 (in front of the EXPRESS stops) when priority is ignored; the
# previous implementation therefore returned (S1, E2, E1).
STRICT_SET = [
    parcel("E1", 10, 0, we="10:00:00", priority="EXPRESS"),
    parcel("E2", 10, 10, we="11:00:00", priority="EXPRESS"),
    parcel("S1", 2, 3, we="12:00:00"),
]


def test_standard_is_never_placed_before_express_even_when_that_is_shorter():
    r = route(STRICT_SET, start="08:00:00", end="18:00:00")
    assert r.parcel_ids == ("E2", "E1", "S1")
    assert_strict_priority(r)
    assert r.unsequenced == ()


def test_the_unrestricted_position_would_really_have_been_shorter():
    # Guards the test above: S1 in front of the EXPRESS stops is a shorter tour than the strict one.
    pts = {p.parcel_id: (p.dest_lat, p.dest_lon) for p in STRICT_SET}

    def length(order):
        path = [(0, 0)] + [pts[i] for i in order] + [(0, 0)]
        return sum(haversine_km(*a, *b) for a, b in zip(path, path[1:]))

    assert length(("S1", "E2", "E1")) < length(("E2", "E1", "S1")) - 0.5
    assert route(STRICT_SET, start="08:00:00", end="18:00:00").total_km == pytest.approx(
        length(("E2", "E1", "S1")), abs=0.011)


def test_standard_insertion_cannot_change_the_first_express_stop():
    express_only = route(STRICT_SET[:2], start="08:00:00", end="18:00:00")
    with_standard = route(STRICT_SET, start="08:00:00", end="18:00:00")
    assert with_standard.stops[0] == express_only.stops[0]
    assert with_standard.stops[1] == express_only.stops[1]  # the whole EXPRESS block is unchanged
    assert with_standard.stops[0].parcel_id == "E2" and with_standard.stops[0].priority == "EXPRESS"


def test_standard_parcels_go_only_at_or_after_the_express_block_boundary():
    r = route(STRICT_SET + [parcel("S2", 8, 1, we="12:00:00"), parcel("S3", 1, 1, we="13:00:00")],
              start="08:00:00", end="18:00:00")
    assert_strict_priority(r)
    assert r.parcel_ids[:2] == ("E2", "E1") and set(r.parcel_ids[2:]) == {"S1", "S2", "S3"}


def test_multiple_express_earlier_window_end_wins():
    # Shift 09:00-09:00 (back by 10:00); parcels on opposite sides: only ONE fits.
    urgent = parcel("E2", east=6, we="10:00:00", priority="EXPRESS")
    relaxed = parcel("E1", east=-5, we="11:00:00", priority="EXPRESS")  # lower id and nearer
    r = route([relaxed, urgent], start="09:00:00", end="09:00:00")
    assert r.parcel_ids == ("E2",) and [u.parcel_id for u in r.unsequenced] == ["E1"]


def test_multiple_express_equal_window_end_lowest_parcel_id_wins():
    a = parcel("E-001", east=-5, we="11:00:00", priority="EXPRESS")
    b = parcel("E-002", east=5, we="11:00:00", priority="EXPRESS")
    r = route([b, a], start="09:00:00", end="09:00:00")
    assert r.parcel_ids == ("E-001",)


def test_multiple_standard_earlier_window_end_wins_with_an_express_stop_present():
    e = parcel("E1", east=1, we="12:00:00", priority="EXPRESS")
    urgent = parcel("S2", north=6, we="10:00:00")
    relaxed = parcel("S1", north=-5, we="11:00:00")  # lower id, nearer, later window_end; other side of the hub
    r = route([relaxed, urgent, e], start="09:00:00", end="09:00:00")  # back by 10:00: only ONE of S1/S2 fits
    assert r.parcel_ids == ("E1", "S2") and [u.parcel_id for u in r.unsequenced] == ["S1"]


def test_multiple_standard_equal_window_end_lowest_parcel_id_wins():
    a = parcel("S-001", east=-5, we="11:00:00")
    b = parcel("S-002", east=5, we="11:00:00")
    assert route([b, a], start="09:00:00", end="09:00:00").parcel_ids == ("S-001",)


def test_standard_block_uses_nearest_insertion_after_the_express_block():
    # EXPRESS E1 at 1 km east, then INSERTION_SET (see above) inserted by nearest insertion
    # starting from E1 (not from the hub): d(E1, .) is used for the first STANDARD position.
    e = parcel("E1", east=1, we="09:30:00", priority="EXPRESS")
    r = route([e] + INSERTION_SET, start="08:00:00", end="18:00:00")
    assert_strict_priority(r)
    assert r.parcel_ids[0] == "E1" and sorted(r.parcel_ids[1:]) == ["P1", "P2", "P3"]
    assert r.stops[1].from_id == "E1"
    assert r.unsequenced == ()


def test_express_first_stop_rule_is_checked_for_every_candidate_position():
    # E1 (7 km) is first at 09:28. E2 (8 km) is cheapest in FRONT (tie -> earlier position) but
    # would then be the first stop at 09:32 > 09:30, so it must go behind E1 instead.
    e1 = parcel("E1", east=7, we="10:00:00", priority="EXPRESS")
    e2 = parcel("E2", east=8, we="12:00:00", priority="EXPRESS")
    r = route([e2, e1], start="09:00:00", end="18:00:00")
    assert r.parcel_ids == ("E1", "E2") and r.stops[0].eta == T("09:28:00") and r.unsequenced == ()


def test_failed_express_stays_unsequenced_and_is_not_moved_behind_standard():
    e = parcel("E1", east=7.51, we="12:00:00", priority="EXPRESS")  # first stop 09:30:02.4
    s = parcel("S1", east=1, we="12:00:00")
    r = route([s, e], start="09:00:00", end="18:00:00")
    assert r.parcel_ids == ("S1",)
    assert [(u.parcel_id, u.reason_code) for u in r.unsequenced] == [("E1", EXPRESS_FIRST_STOP_AFTER_0930)]
    assert r.status == "PARTIAL"


def test_no_express_sequenced_means_standard_route_ignores_the_0930_rule():
    e = parcel("E1", east=1, we="12:00:00", priority="EXPRESS")
    s = parcel("S1", east=1, we="12:00:00")
    r = route([e, s], start="10:00:00", end="18:00:00")  # EXPRESS cannot be first after 09:30
    assert r.parcel_ids == ("S1",) and r.stops[0].eta > T("09:30:00")
    assert r.unsequenced[0].parcel_id == "E1" and r.unsequenced[0].reason_code == EXPRESS_FIRST_STOP_AFTER_0930


def test_express_first_stop_exactly_0930_with_standard_behind_it():
    r = route([parcel("S1", east=2, we="12:00:00"), parcel("E1", east=7.5, priority="EXPRESS")], start="09:00:00")
    assert r.parcel_ids[0] == "E1" and r.stops[0].eta == T("09:30:00") and r.parcel_ids[1:] == ("S1",)


def test_standard_that_would_break_the_return_limit_is_refused_but_express_block_is_kept():
    e = parcel("E1", east=6, we="12:00:00", priority="EXPRESS")
    s = parcel("S1", east=-5, we="12:00:00")  # nearer to nothing useful: pushes the return past 10:00
    r = route([s, e], start="09:00:00", end="09:00:00")
    assert r.parcel_ids == ("E1",) and r.unsequenced[0].parcel_id == "S1"
    assert r.unsequenced[0].reason_code == RETURN_AFTER_SHIFT_END_PLUS_60


def test_strict_priority_result_does_not_depend_on_input_order():
    ps = STRICT_SET + [parcel("S2", 8, 1, we="12:00:00"), parcel("E3", 4, -2, we="11:00:00", priority="EXPRESS"),
                       parcel("S3", -3, 4, we="13:00:00")]
    expected = route(ps, start="08:00:00", end="18:00:00")
    rng = random.Random(7)
    for _ in range(25):
        shuffled = ps[:]
        rng.shuffle(shuffled)
        assert route(shuffled, start="08:00:00", end="18:00:00") == expected
    assert_strict_priority(expected)


def test_strict_priority_holds_for_many_random_mixed_sets():
    rng = random.Random(2026)
    for _ in range(60):
        ps = [parcel(f"P{i:02d}", rng.uniform(-12, 12), rng.uniform(-12, 12),
                     ws=f"{rng.choice([8, 9, 10]):02d}:00:00", we=f"{rng.choice([10, 12, 14]):02d}:00:00",
                     priority=rng.choice(["EXPRESS", "STANDARD"]), cod=rng.random() < 0.3)
              for i in range(rng.randint(1, 9))]
        r = route(ps, start="08:30:00", end="16:00:00")
        assert_strict_priority(r)
        assert sorted(r.parcel_ids + tuple(u.parcel_id for u in r.unsequenced)) == sorted(p.parcel_id for p in ps)
        for s in r.stops:  # every kept stop satisfies its window and the full-route timing chain
            assert s.window_start <= s.eta <= s.window_end
        for a, b in zip(r.stops, r.stops[1:]):
            assert b.arrival >= a.departure
        assert r.return_time <= T("16:00:00") + 60 * MIN


# ---- 7-10. windows -----------------------------------------------------------------

def test_early_arrival_waits_until_window_start():
    r = route([parcel("P1", east=2.5, ws="10:00:00", we="12:00:00")], start="09:00:00")
    s = r.stops[0]
    assert s.arrival == T("09:10:00") and s.wait == 50 * MIN and s.eta == T("10:00:00")
    assert s.departure == T("10:03:00") and r.return_time == T("10:13:00")


def test_arrival_exactly_at_window_start_is_accepted():
    s = route([parcel("P1", east=2.5, ws="09:10:00")], start="09:00:00").stops[0]
    assert s.eta == T("09:10:00") and s.wait == timedelta(0)


def test_arrival_exactly_at_window_end_is_accepted():
    r = route([parcel("P1", east=15, we="10:00:00")], start="09:00:00", end="12:00:00")  # 60 min away
    assert r.stops[0].eta == T("10:00:00") and r.unsequenced == ()


def test_late_arrival_is_unsequenced_with_reason_not_clamped():
    r = route([parcel("P1", east=15.01, we="10:00:00")], start="09:00:00", end="12:00:00")  # 2.4 s late
    assert r.stops == () and r.status == "PARTIAL"
    (u,) = r.unsequenced
    assert u.parcel_id == "P1" and u.reason_code == LATE_FOR_WINDOW
    assert "10:00:00" in u.detail
    assert r.return_time == T("09:00:00") and r.total_km == 0.0  # empty route stays valid


# ---- 11-13. EXPRESS 09:30 rule -------------------------------------------------------

def test_express_first_stop_exactly_0930_is_accepted():
    r = route([parcel("E1", east=7.5, priority="EXPRESS")], start="09:00:00")
    assert r.parcel_ids == ("E1",) and r.stops[0].eta == T("09:30:00")


def test_express_first_stop_after_0930_is_rejected():
    r = route([parcel("E1", east=7.51, priority="EXPRESS")], start="09:00:00")  # 09:30:02.4
    assert r.stops == ()
    assert r.unsequenced[0].reason_code == EXPRESS_FIRST_STOP_AFTER_0930


def test_express_rejected_when_shift_starts_after_0930():
    r = route([parcel("E1", east=1, priority="EXPRESS")], start="10:00:00")
    assert r.unsequenced[0].reason_code == EXPRESS_FIRST_STOP_AFTER_0930


def test_standard_first_stop_after_0930_is_not_rejected_by_the_express_rule():
    assert route([parcel("S1", east=7.51)], start="09:00:00").parcel_ids == ("S1",)
    assert route([parcel("S1", east=1)], start="10:00:00").parcel_ids == ("S1",)


# ---- 14. COD service ------------------------------------------------------------------

def test_cod_stop_adds_five_minutes_of_service():
    cod = parcel("C1", east=3, we="10:00:00", cod=True)
    plain = parcel("P2", east=6, we="11:00:00")
    r = route([cod, plain], start="09:00:00")
    by_id = {s.parcel_id: s for s in r.stops}
    assert by_id["C1"].service == 8 * MIN and by_id["C1"].departure - by_id["C1"].eta == 8 * MIN
    assert by_id["P2"].service == 3 * MIN
    # order P2 -> C1: legs 6 + 3 + 3 km = 48 min travel, 3 + 8 min service
    assert r.parcel_ids == ("P2", "C1") and r.return_time == T("09:59:00")


# ---- 15-17. shift-end boundary ---------------------------------------------------------

def test_return_exactly_at_shift_end_is_accepted():
    # README EC-03: 3.00 km and 6.75 km on one ray; 13.50 km = 54 min + 2 * 3 min = 60 min.
    r = route([parcel("PCL-00501", east=3.0, ws="09:00:00", we="10:00:00"),
               parcel("PCL-00502", east=6.75, ws="09:00:00", we="10:00:00")], start="09:00:00", end="10:00:00")
    assert len(r.stops) == 2 and r.total_km == 13.50
    assert r.return_time == T("10:00:00") and r.uses_overtime is False


def test_return_exactly_at_shift_end_plus_60_is_accepted():
    # 14 km each way = 112 min, COD service 8 min -> back at 10:00:00 = 09:00:00 + 60 min.
    r = route([parcel("P1", east=14, cod=True)], start="08:00:00", end="09:00:00")
    assert r.return_time == T("10:00:00") and r.uses_overtime is True and r.unsequenced == ()


def test_return_one_microsecond_after_shift_end_plus_60_is_rejected():
    ps = [parcel("P1", east=14, cod=True)]  # returns at exactly 10:00:00
    r = sequence_route(0, 0, "08:00:00", time(8, 59, 59, 999_999), ps)  # deadline 09:59:59.999999
    assert r.stops == () and r.unsequenced[0].reason_code == RETURN_AFTER_SHIFT_END_PLUS_60


def test_return_a_few_seconds_late_is_rejected():
    r = route([parcel("P1", east=14.01, cod=True)], start="08:00:00", end="09:00:00")
    assert r.unsequenced[0].reason_code == RETURN_AFTER_SHIFT_END_PLUS_60


# ---- 18. insertion recalculates downstream timing ------------------------------------------

def test_insertion_recalculates_downstream_arrival_and_wait():
    a = parcel("A", east=6, ws="10:00:00")
    b = parcel("B", east=3)
    alone = route([a]).stops[0]
    assert alone.arrival == T("09:24:00") and alone.wait == 36 * MIN
    both = route([a, b])
    assert both.parcel_ids == ("B", "A")        # B inserted in front (equal cost, earlier position)
    b_stop, a_stop = both.stops
    assert b_stop.eta == T("09:12:00") and b_stop.departure == T("09:15:00")
    # A now arrives later (09:27) and waits less (33 min); its ETA is still its window start.
    assert (a_stop.arrival, a_stop.wait, a_stop.eta, a_stop.departure) == (
        T("09:27:00"), 33 * MIN, T("10:00:00"), T("10:03:00"))


def test_insertion_that_makes_a_later_stop_late_is_skipped():
    a = parcel("A", east=6, we="09:24:00")      # reached at exactly 09:24:00 when first
    b = parcel("B", east=3, we="12:00:00")
    r = route([a, b])
    # In front of A, B would push A to 09:27 (late), so B must go after A.
    assert r.parcel_ids == ("A", "B") and r.unsequenced == ()
    assert r.stops[0].eta == T("09:24:00")


# ---- 19. empty route -------------------------------------------------------------------------

def test_empty_parcel_list():
    r = route([])
    assert r.stops == () and r.unsequenced == () and r.status == "COMPLETE"
    assert (r.return_from_id, r.return_leg_km, r.total_km) == ("HUB", 0.0, 0.0)
    assert r.return_time == T("09:00:00") and r.uses_overtime is False


def test_no_parcel_is_silently_dropped():
    ps = [parcel(f"P{i}", east=-6 + 3 * i, we="10:00:00") for i in range(1, 5)] + [parcel("FAR", east=500)]
    r = route(ps, start="09:00:00", end="10:00:00")
    assert sorted(r.parcel_ids + tuple(u.parcel_id for u in r.unsequenced)) == sorted(p.parcel_id for p in ps)
    assert "FAR" in [u.parcel_id for u in r.unsequenced]


# ---- 20. invalid input ----------------------------------------------------------------------------

def test_duplicate_parcel_ids_rejected():
    with pytest.raises(ValueError, match="duplicate parcel_id 'P1'"):
        route([parcel("P1", 1), parcel("P1", 2)])


@pytest.mark.parametrize("bad_id", ["", 123, None])
def test_parcel_id_must_be_a_non_empty_string(bad_id):
    p = parcel("P1")
    bad = Parcel(**{**p.__dict__, "parcel_id": bad_id})

    with pytest.raises(ValueError, match="parcel_id"):
        route([bad])


def test_parcel_id_clashing_with_hub_id_rejected():
    with pytest.raises(ValueError, match="hub"):
        route([parcel("HUB", 1)])


def test_invalid_priority_rejected():
    with pytest.raises(ValueError, match="priority"):
        route([parcel("P1", 1, priority="URGENT")])


@pytest.mark.parametrize("bad", ["true", 1, None])
def test_cod_flag_must_be_a_bool(bad):
    with pytest.raises(TypeError, match="cod_flag"):
        route([parcel("P1", 1, cod=bad)])


def test_window_end_before_window_start_rejected():
    with pytest.raises(ValueError, match="window_end"):
        route([parcel("P1", 1, ws="12:00:00", we="09:00:00")])


def test_invalid_parcel_coordinates_rejected_with_parcel_id():
    bad = parcel("P1", 1)
    bad = Parcel(**{**bad.__dict__, "dest_lat": Decimal("95")})
    with pytest.raises(ValueError, match="P1"):
        route([bad])


def test_wrong_input_types_rejected():
    with pytest.raises(TypeError):
        route([{"parcel_id": "P1"}])
    with pytest.raises(TypeError):
        route("P1")


def test_bad_shift_or_hub_rejected():
    with pytest.raises(ValueError, match="shift_end"):
        sequence_route(0, 0, "10:00:00", "09:00:00", [])
    with pytest.raises(ValueError):
        sequence_route(0, 0, "9am", "17:00:00", [])
    with pytest.raises(ValueError):
        sequence_route(91, 0, "09:00:00", "17:00:00", [])


def test_any_iterable_of_parcels_is_accepted():
    assert route(iter([parcel("P1", 1)])).parcel_ids == ("P1",)


# ---- midnight ---------------------------------------------------------------------------------------

def test_stop_reached_after_midnight_is_late_not_wrapped():
    r = route([parcel("P1", east=5, ws="23:00:00", we="23:59:59")], start="23:55:00", end="23:59:59")
    assert r.stops == () and r.unsequenced[0].reason_code == LATE_FOR_WINDOW


def test_return_after_midnight_is_kept_as_over_24_hours():
    r = route([parcel("P1", east=1, ws="23:00:00", we="23:59:59", cod=True)], start="23:50:00", end="23:55:00")
    assert r.return_time == timedelta(hours=24, minutes=6)  # 23:54 + 8 min service + 4 min back; not 00:06


# ---- official sample data (read-only) --------------------------------------------------------------------

DATA = Path(__file__).resolve().parent.parent / "data"
needs_data = pytest.mark.skipif(not (DATA / "parcels.csv").is_file(), reason="sample data not present")


@needs_data
def test_official_ec03_route_returns_exactly_at_shift_end():
    ds = load_dataset(DATA)
    hub = ds.hubs[0]
    shift = next(s for s in ds.shifts if s.rider_id == "RDR-03" and s.shift_date == date(2026, 10, 14))
    ps = [p for p in ds.parcels if p.parcel_id in ("PCL-00501", "PCL-00502")]
    r = sequence_route(hub.lat, hub.lon, shift.shift_start, shift.shift_end, ps, hub_id=hub.hub_id)
    assert len(r.stops) == 2 and r.unsequenced == ()
    assert r.total_km == 13.50 and r.return_time == T("10:00:00")


@needs_data
def test_official_ec02_express_first_stop_exactly_0930():
    ds = load_dataset(DATA)
    hub = ds.hubs[0]
    shift = next(s for s in ds.shifts if s.rider_id == "RDR-08" and s.shift_date == date(2026, 10, 13))
    ps = [p for p in ds.parcels if p.parcel_id == "PCL-00251"]
    r = sequence_route(hub.lat, hub.lon, shift.shift_start, shift.shift_end, ps, hub_id=hub.hub_id)
    assert r.stops[0].leg_km == 7.50 and format_hhmmss(r.stops[0].eta) == "09:30:00"


# ---- 21. local only ----------------------------------------------------------------------------------------

def test_module_imports_only_standard_library_and_project_modules():
    tree = ast.parse(Path(rs_module.__file__).read_text(encoding="utf-8"))
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
    assert route([parcel("P1", east=3)]).parcel_ids == ("P1",)
