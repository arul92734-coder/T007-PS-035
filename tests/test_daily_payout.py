"""Unit tests for ps035.daily_payout (BR-05 .. BR-09) on small hand-made scenarios.

Records are the real loader records (Parcel, DeliveryEvent, ...), the real
EventLog and the real RouteResult; nothing is mocked. The official sample pack is
tested separately in test_daily_payout_sample.py.
"""
from __future__ import annotations

from dataclasses import fields
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal

import pytest

from ps035 import daily_payout as dp
from ps035.daily_payout import (
    LATE_EXCLUDE, DailyPayoutResult, PayoutContext, PayoutPolicy,
    calculate_daily_payout, calculate_daily_payouts, derive_planned_route,
    planned_overtime_km, riders_for_date,
)
from ps035.delivery_events import EventLog
from ps035.eta import time_of_day, travel_duration
from ps035.loader import (
    CodDeposit, DataSet, DeliveryEvent, Hub, OdometerReading, Parcel, Rider, RiderShift,
    p_time, p_timestamp,
)
from ps035.route_sequencing import RouteResult, RouteStop

D = date(2026, 10, 12)
D0 = D - timedelta(days=1)
D2 = Decimal


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------

HUB = Hub("HUB-01", "Test Hub", D2("12.903000"), D2("77.624000"))
RIDERS = [
    Rider("RDR-01", "Van", "VAN", 120, D2("300.0"), "HUB-01", p_time("09:00:00"), p_time("18:00:00")),
    Rider("RDR-02", "Bike", "BIKE", 40, D2("25.0"), "HUB-01", p_time("09:00:00"), p_time("18:00:00")),
]


def plain_route(km, start="09:00:00", end="18:00:00") -> RouteResult:
    """A real RouteResult for a straight out-and-back of ``km`` (no stops)."""
    s, e = time_of_day(start), time_of_day(end)
    back = s + travel_duration(float(km))
    return RouteResult(hub_id="HUB-01", shift_start=s, shift_end=e, stops=(), return_from_id="HUB-01",
                       return_leg_km=float(km), return_time=back, total_km=float(km),
                       uses_overtime=back > e, unsequenced=())


class Scn:
    """Collects records, then builds a PayoutContext (EventLog validates the history)."""

    def __init__(self):
        self.parcels: list[Parcel] = []
        self.events: list[DeliveryEvent] = []
        self.odo: list[OdometerReading] = []
        self.deposits: list[CodDeposit] = []
        self.shifts = [RiderShift("RDR-01", D, p_time("09:00:00"), p_time("18:00:00"))]
        self._p = self._e = self._d = 0

    def parcel(self, priority="STANDARD", window_end="23:00:00", cod="0.00", pool=D) -> str:
        self._p += 1
        pid = f"PCL-{self._p:05d}"
        self.parcels.append(Parcel(pid, pool, D2("12.918566"), D2("77.614755"), D2("1.0"), p_time("00:00:00"),
                                   p_time(window_end), priority, D2(cod) > 0, D2(cod)))
        return pid

    def _event(self, pid, etype, ts, day, reason="", attempt=1, cod="0.00", rider="RDR-01") -> str:
        self._e += 1
        eid = f"EVT-{self._e:05d}"
        self.events.append(DeliveryEvent(eid, day, pid, rider, p_timestamp(f"{day} {ts}"), etype, reason,
                                         attempt, D2(cod)))
        return eid

    def delivered(self, pid, ts="10:00:00", attempt=1, cod="0.00", rider="RDR-01", day=D):
        return self._event(pid, "DELIVERED", ts, day, "", attempt, cod, rider)

    def failed(self, pid, reason, ts="10:00:00", attempt=1, rider="RDR-01", day=D):
        return self._event(pid, "FAILED", ts, day, reason, attempt, "0.00", rider)

    def resched(self, pid, ts="10:00:00", attempt=1, rider="RDR-01", day=D):
        return self._event(pid, "RESCHEDULED", ts, day, "", attempt, "0.00", rider)

    def complaint(self, pid, ts="11:00:00", rider="RDR-01", day=D):
        return self._event(pid, "COMPLAINT", ts, day, "RUDE_BEHAVIOUR", 1, "0.00", rider)

    def many(self, n, priority="STANDARD", **kw) -> list[str]:
        out = []
        for _ in range(n):
            pid = self.parcel(priority)
            self.delivered(pid, **kw)
            out.append(pid)
        return out

    def second_attempt(self, rider="RDR-01"):
        """A parcel rescheduled the day before and delivered today (attempt 2)."""
        pid = self.parcel("STANDARD", pool=D0)
        self.resched(pid, day=D0, rider=rider)
        self.delivered(pid, attempt=2, rider=rider)
        return pid

    def odometer(self, start="1000.00", end="1020.00", rider="RDR-01", day=D):
        self.odo.append(OdometerReading(rider, day, D2(start), D2(end)))

    def deposit(self, amount, ts="18:00:00", rider="RDR-01", day=D):
        self._d += 1
        self.deposits.append(CodDeposit(f"DEP-{self._d:04d}", rider, day, p_timestamp(f"{day} {ts}"), D2(amount)))

    def ctx(self) -> PayoutContext:
        ds = DataSet(hubs=[HUB], riders=RIDERS, shifts=self.shifts, parcels=self.parcels, events=self.events,
                     odometer=self.odo, cod_deposits=self.deposits)
        return PayoutContext.from_dataset(ds)

    def pay(self, route="none", rider="RDR-01", **kw) -> DailyPayoutResult:
        if route == "none":
            route = None
        return calculate_daily_payout(self.ctx(), rider, D, planned_route=route, **kw)


def codes(result, kind=None):
    return [i.code for i in result.line_items if kind is None or i.kind == kind]


# --------------------------------------------------------------------------
# BR-05 per-parcel pay
# --------------------------------------------------------------------------


def test_standard_and_express_delivered_pay():
    s = Scn()
    s.many(3, "STANDARD")
    s.many(2, "EXPRESS")
    r = s.pay()
    assert (r.delivered_count, r.standard_delivered_count, r.express_delivered_count) == (5, 3, 2)
    assert r.delivery_pay_standard == D2(54) and r.delivery_pay_express == D2(50)
    assert r.delivery_pay == D2(104)
    assert all(isinstance(i.amount, Decimal) for i in r.line_items)


@pytest.mark.parametrize("reason,pay,paid", [("NOT_HOME", 10, 1), ("WRONG_ADDRESS", 10, 1),
                                             ("RIDER_FAULT", 0, 0), ("CUSTOMER_BUSY", 0, 0)])
def test_failed_attempt_pay_by_reason(reason, pay, paid):
    s = Scn()
    s.failed(s.parcel(), reason)
    r = s.pay()
    assert r.failed_attempt_pay == D2(pay)
    assert r.failed_paid_count == paid
    assert r.failed_unpaid_count == 1 - paid
    assert r.delivered_count == 0


def test_customer_busy_is_reported_not_paid():
    s = Scn()
    s.failed(s.parcel(), "CUSTOMER_BUSY")
    r = s.pay()
    assert dp.WARNING_UNRECOGNISED_REASON in r.warning_codes
    assert r.gross_earnings == D2(0) and r.status == dp.STATUS_REVIEW


def test_rescheduled_earns_nothing_and_is_not_delivered():
    s = Scn()
    s.resched(s.parcel())
    r = s.pay()
    assert r.rescheduled_count == 1 and r.delivered_count == 0
    assert r.gross_earnings == D2(0)
    assert dp.CODE_RESCHEDULED in codes(r, dp.INFO)


def test_attempts_on_different_days_pay_each_outcome_once():
    # day 1: attempt 1 FAILED NOT_HOME then RESCHEDULED (same attempt); day 2: attempt 2 DELIVERED.
    s = Scn()
    pid = s.parcel("STANDARD", pool=D0)
    s.failed(pid, "NOT_HOME", day=D0)
    s.resched(pid, ts="10:05:00", day=D0)
    s.delivered(pid, attempt=2)
    ctx = s.ctx()
    day1 = calculate_daily_payout(ctx, "RDR-01", D0, planned_route=None)
    day2 = calculate_daily_payout(ctx, "RDR-01", D, planned_route=None)
    assert day1.failed_paid_count == 1 and day1.failed_attempt_pay == D2(10)
    assert day1.rescheduled_count == 0                      # superseded by the FAILED outcome of attempt 1
    assert dp.WARNING_DUPLICATE_EVENT not in day1.warning_codes   # FAILED + RESCHEDULED is normal
    assert day2.delivered_count == 1 and day2.delivery_pay == D2(18)   # paid once, at the registered priority


def test_upgraded_priority_is_not_paid_as_express_by_default_but_policy_can_change_it():
    s = Scn()
    s.second_attempt()
    ctx = s.ctx()
    default = calculate_daily_payout(ctx, "RDR-01", D, planned_route=None)
    upgraded = calculate_daily_payout(ctx, "RDR-01", D, planned_route=None,
                                      policy=PayoutPolicy(pay_upgraded_priority_as_express=True))
    assert default.delivery_pay == D2(18) and default.standard_delivered_count == 1
    assert upgraded.delivery_pay == D2(25) and upgraded.express_delivered_count == 1


def test_duplicate_outcomes_for_one_attempt_pay_once():
    # EventLog refuses duplicate outcomes, so check the payable-event selection directly.
    s = Scn()
    pid = s.parcel()
    ev_fail = DeliveryEvent("EVT-00001", D, pid, "RDR-01", p_timestamp(f"{D} 09:00:00"), "FAILED", "NOT_HOME", 1, D2(0))
    ev_del = DeliveryEvent("EVT-00002", D, pid, "RDR-01", p_timestamp(f"{D} 09:30:00"), "DELIVERED", "", 1, D2(0))
    ev_del2 = DeliveryEvent("EVT-00003", D, pid, "RDR-01", p_timestamp(f"{D} 09:40:00"), "DELIVERED", "", 1, D2(0))
    chosen, complaints, ignored = dp._outcome_events([ev_fail, ev_del, ev_del2])
    assert chosen == [ev_del] and complaints == []
    assert {i.event_id for i, _ in ignored} == {"EVT-00001", "EVT-00003"}


def test_duplicate_complaint_event_id_is_counted_once():
    s = Scn()
    pid = s.parcel()
    c = DeliveryEvent("EVT-00009", D, pid, "RDR-01", p_timestamp(f"{D} 09:00:00"), "COMPLAINT", "X", 1, D2(0))
    _, complaints, _ = dp._outcome_events([c, c])
    assert complaints == [c]


# --------------------------------------------------------------------------
# BR-06 distance
# --------------------------------------------------------------------------


def test_distance_pay_is_3_50_per_km():
    s = Scn()
    s.odometer("1000.00", "1020.00")
    r = s.pay(plain_route(20))
    assert r.distance_source == dp.DISTANCE_SOURCE_ODOMETER
    assert r.selected_distance_km == D2("20.00")
    assert r.regular_distance_pay == D2("70.00") and r.overtime_distance_pay == D2(0)
    assert r.gross_earnings == D2("70.00")


def test_odometer_below_planned_within_symmetric_tolerance_is_used():
    s = Scn()
    s.odometer("1000.00", "1019.00")  # 5% below planned 20 km
    r = s.pay(plain_route(20))
    assert r.selected_distance_km == D2("19.00") and not r.odometer_discrepancy_flag
    assert r.regular_distance_pay == D2("66.50")


@pytest.mark.parametrize("end,used,flag,source", [
    ("1022.00", "22.00", False, dp.DISTANCE_SOURCE_ODOMETER),   # exactly +10%: accepted
    ("1022.01", "20.00", True, dp.DISTANCE_SOURCE_PLANNED),     # > +10%: planned + flag
    ("1021.99", "21.99", False, dp.DISTANCE_SOURCE_ODOMETER),   # within upper tolerance
    ("1018.00", "18.00", False, dp.DISTANCE_SOURCE_ODOMETER),   # exactly -10%: accepted
    ("1017.99", "20.00", True, dp.DISTANCE_SOURCE_PLANNED),    # > -10%: planned + flag
    ("1018.01", "18.01", False, dp.DISTANCE_SOURCE_ODOMETER),  # within lower tolerance
])
def test_odometer_tolerance_boundary(end, used, flag, source):
    s = Scn()
    s.odometer("1000.00", end)
    r = s.pay(plain_route(20))
    assert r.selected_distance_km == D2(used)
    assert r.odometer_discrepancy_flag is flag
    assert r.distance_source == source
    assert (dp.WARNING_ODOMETER_DISCREPANCY in r.warning_codes) is flag


def test_odometer_far_below_planned_is_rejected_and_flagged():
    s = Scn()
    s.odometer("1000.00", "1015.00")  # 25% below planned 20 km
    r = s.pay(plain_route(20))
    assert r.selected_distance_km == D2("20.00")
    assert r.distance_source == dp.DISTANCE_SOURCE_PLANNED
    assert r.odometer_discrepancy_flag
    assert dp.WARNING_ODOMETER_DISCREPANCY in r.warning_codes


def test_missing_odometer_uses_planned_and_reports():
    s = Scn()
    r = s.pay(plain_route(20))
    assert r.odometer_distance_km is None
    assert r.selected_distance_km == D2("20.00") and r.distance_source == dp.DISTANCE_SOURCE_PLANNED
    assert dp.WARNING_MISSING_ODOMETER in r.warning_codes and r.status == dp.STATUS_REVIEW
    assert not r.odometer_discrepancy_flag


def test_missing_route_pays_no_distance_and_never_invents_one():
    s = Scn()
    s.odometer("1000.00", "1020.00")
    r = s.pay(None)
    assert r.planned_distance_km is None and r.selected_distance_km is None
    assert r.distance_source == dp.DISTANCE_SOURCE_NONE
    assert r.regular_distance_pay == D2(0) and r.overtime_distance_pay == D2(0)
    assert dp.WARNING_NO_PLANNED_ROUTE in r.warning_codes
    assert not r.odometer_discrepancy_flag and r.odometer_distance_km == D2("20.00")


def test_route_with_no_stops_and_unsequenced_parcels_is_not_a_planned_distance():
    from ps035.route_sequencing import UnsequencedParcel
    base = plain_route(0)
    empty = RouteResult(**{**base.__dict__, "unsequenced": (UnsequencedParcel("PCL-00001", "LATE_FOR_WINDOW", "x"),)})
    r = Scn().pay(empty)
    assert r.planned_distance_km is None and dp.WARNING_NO_PLANNED_ROUTE in r.warning_codes


def test_zero_distance_with_no_odometer_issue():
    s = Scn()
    s.odometer("1000.00", "1000.00")
    r = s.pay(plain_route(0))
    assert r.selected_distance_km == D2("0.00") and r.regular_distance_pay == D2(0)
    assert not r.odometer_discrepancy_flag


def test_distance_pay_rounds_half_up_to_paise_only():
    s = Scn()
    s.odometer("1000.00", "1000.03")
    r = s.pay(plain_route(0.03))
    assert r.regular_distance_pay == D2("0.11")         # 0.03 x 3.50 = 0.105 -> 0.11


def _overtime_route() -> RouteResult:
    """Shift 09:00-10:00. Legs: 5 km (09:00-09:20), 10 km (09:23-10:03), return 5 km (10:06-10:26)."""
    m = lambda h, mi: timedelta(hours=h, minutes=mi)
    def stop(seq, pid, frm, km, arr, dep):
        return RouteStop(seq, pid, frm, km, "STANDARD", False, m(0, 0), m(23, 0), arr, m(0, 0), arr, m(0, 3), dep)
    return RouteResult(hub_id="HUB-01", shift_start=m(9, 0), shift_end=m(10, 0),
                       stops=(stop(1, "PCL-00001", "HUB-01", 5.0, m(9, 20), m(9, 23)),
                              stop(2, "PCL-00002", "PCL-00001", 10.0, m(10, 3), m(10, 6))),
                       return_from_id="PCL-00002", return_leg_km=5.0, return_time=m(10, 26), total_km=20.0,
                       uses_overtime=True, unsequenced=())


def test_planned_overtime_km_splits_the_crossing_leg_by_time():
    # 10 km leg driven 09:23-10:03, 3 of 40 minutes after 10:00 -> 0.75 km; whole return leg 5 km.
    assert planned_overtime_km(_overtime_route()) == D2("5.75")


def test_planned_overtime_km_zero_when_back_by_shift_end_and_full_when_all_after():
    assert planned_overtime_km(plain_route(20)) == D2("0.00")
    late = plain_route(10, start="18:00:00", end="18:00:00")      # drives only after shift end
    assert planned_overtime_km(late) == D2("10.00")
    exact = plain_route(15, start="09:00:00", end="10:00:00")     # returns exactly at shift end
    assert exact.return_time == time_of_day("10:00:00") and planned_overtime_km(exact) == D2("0.00")


def test_overtime_split_pays_each_km_once_with_1_5x_on_overtime():
    s = Scn()
    s.odometer("1000.00", "1020.00")
    r = s.pay(_overtime_route())
    assert (r.regular_distance_km, r.overtime_distance_km) == (D2("14.25"), D2("5.75"))
    assert r.regular_distance_km + r.overtime_distance_km == r.selected_distance_km    # no double count
    assert r.overtime_rate_per_km == D2("5.25")
    assert r.regular_distance_pay == D2("49.88")        # 14.25 x 3.50 = 49.875
    assert r.overtime_distance_pay == D2("30.19")       # 5.75 x 5.25  = 30.1875
    assert r.gross_earnings == r.regular_distance_pay + r.overtime_distance_pay


def test_overtime_share_is_applied_to_an_accepted_odometer_distance():
    s = Scn()
    s.odometer("1000.00", "1018.00")                    # 18 km, within tolerance
    r = s.pay(_overtime_route())
    assert r.selected_distance_km == D2("18.00")
    assert r.overtime_distance_km == D2("5.18")         # 18 x 5.75 / 20 = 5.175 -> 5.18
    assert r.regular_distance_km == D2("12.82")
    assert r.regular_distance_pay == D2("44.87") and r.overtime_distance_pay == D2("27.20")


def test_overtime_uses_planned_split_when_odometer_is_rejected():
    s = Scn()
    s.odometer("1000.00", "1030.00")                    # +50%: planned used, flag set
    r = s.pay(_overtime_route())
    assert r.odometer_discrepancy_flag and r.selected_distance_km == D2("20.00")
    assert r.overtime_distance_km == D2("5.75")


def test_real_sequencer_route_with_overtime_is_split_consistently():
    from ps035.route_sequencing import sequence_route
    far = Parcel("PCL-00001", D, D2("12.903000"), D2("77.684000"), D2("1.0"), p_time("00:00:00"),
                 p_time("23:00:00"), "STANDARD", False, D2(0))
    route = sequence_route(HUB.lat, HUB.lon, "09:00:00", "09:30:00", [far], hub_id="HUB-01")
    assert route.uses_overtime
    ot = planned_overtime_km(route)
    assert 0 < ot <= Decimal(repr(route.total_km))


# --------------------------------------------------------------------------
# BR-07 incentives
# --------------------------------------------------------------------------


@pytest.mark.parametrize("n,volume", [(0, 0), (29, 0), (30, 100), (44, 100), (45, 250), (60, 250)])
def test_volume_incentive_tiers_are_not_cumulative(n, volume):
    s = Scn()
    s.many(n)
    r = s.pay()
    assert r.volume_incentive == D2(volume)
    assert r.delivered_count == n


@pytest.mark.parametrize("first,second,bonus,pct", [
    (18, 1, 0, "94.74"),     # 18/19 just below 95%
    (19, 1, 50, "95.00"),    # 19/20 exactly 95%
    (29, 1, 50, "96.67"),    # 29/30 above
])
def test_first_attempt_incentive_boundary(first, second, bonus, pct):
    s = Scn()
    s.many(first)
    for _ in range(second):
        s.second_attempt()
    r = s.pay()
    assert (r.first_attempt_successes, r.eligible_attempt_count) == (first, first + second)
    assert r.first_attempt_incentive == D2(bonus)
    assert r.first_attempt_success_rate_pct == D2(pct)


def test_first_attempt_denominator_counts_failures_and_reschedules_but_not_unattempted_parcels():
    s = Scn()
    s.many(19)
    s.failed(s.parcel(), "NOT_HOME")          # attempt-1 failure counts against the rider
    s.parcel()                                # a parcel with no event is not an attempt
    r = s.pay()
    assert (r.first_attempt_successes, r.eligible_attempt_count) == (19, 20) and r.first_attempt_incentive == D2(50)
    s.resched(s.parcel())                     # a reschedule is a missed first attempt by default
    ctx = s.ctx()
    default = calculate_daily_payout(ctx, "RDR-01", D, planned_route=None)
    excl = calculate_daily_payout(ctx, "RDR-01", D, planned_route=None,
                                  policy=PayoutPolicy(reschedule_counts_as_first_attempt_miss=False))
    assert default.eligible_attempt_count == 21 and default.first_attempt_incentive == D2(0)   # 19/21
    assert excl.eligible_attempt_count == 20 and excl.first_attempt_incentive == D2(50)


def test_only_failures_gives_zero_rate_and_no_events_gives_no_rate():
    s = Scn()
    s.failed(s.parcel(), "NOT_HOME")
    s.failed(s.parcel(), "RIDER_FAULT")
    r = s.pay()
    assert (r.first_attempt_successes, r.eligible_attempt_count) == (0, 2)
    assert r.first_attempt_success_rate_pct == D2("0.00") and r.first_attempt_incentive == D2(0)
    empty = Scn().pay()
    assert empty.first_attempt_success_rate_pct is None and empty.first_attempt_incentive == D2(0)


# --------------------------------------------------------------------------
# BR-08 penalties
# --------------------------------------------------------------------------


def test_rider_fault_late_and_complaint_penalties():
    s = Scn()
    s.many(40, ts="10:00:00")                                   # big gross so the cap does not bind
    s.failed(s.parcel(), "RIDER_FAULT")
    s.failed(s.parcel(), "RIDER_FAULT")
    late = s.parcel(window_end="10:00:00")
    s.delivered(late, ts="10:00:01")
    s.complaint(late)
    r = s.pay()
    assert (r.rider_fault_count, r.late_delivery_count, r.complaint_count) == (2, 1, 1)
    assert (r.rider_fault_penalty, r.late_delivery_penalty, r.complaint_penalty) == (D2(100), D2(20), D2(100))
    assert r.penalties_uncapped == D2(220) and r.penalties_deducted == D2(220)
    assert r.net_payout == r.gross_earnings - D2(220)


def test_rider_fault_penalty_is_per_attempt_not_per_event_copy():
    s = Scn()
    s.many(40)
    pid = s.parcel()
    s.failed(pid, "RIDER_FAULT", ts="09:00:00")
    s.resched(pid, ts="09:01:00")                               # same attempt: still one penalty
    assert s.pay().rider_fault_count == 1


@pytest.mark.parametrize("ts,late", [("12:59:59", 0), ("13:00:00", 0), ("13:00:01", 1), ("13:04:35", 1)])
def test_window_end_boundary_for_late_penalty(ts, late):
    s = Scn()
    pid = s.parcel(window_end="13:00:00")
    s.delivered(pid, ts=ts)
    r = s.pay()
    assert r.late_delivery_count == late
    assert r.late_delivery_penalty == D2(20 * late)


def test_early_delivery_before_window_start_is_not_late():
    s = Scn()
    pid = s.parcel(window_end="13:00:00")
    s.delivered(pid, ts="00:00:01")
    assert s.pay().late_delivery_count == 0


def test_failed_attempt_after_window_end_is_not_a_late_delivery():
    s = Scn()
    s.failed(s.parcel(window_end="09:00:00"), "NOT_HOME", ts="17:00:00")
    assert s.pay().late_delivery_count == 0


def _capped(n_express, n_std, n_resched=1):
    """One complaint (100) on a day whose gross is 25*n_express + 18*n_std.

    The rescheduled parcels keep the first-attempt rate under 95%, so no 50 bonus.
    """
    s = Scn()
    delivered = s.many(n_express, "EXPRESS") + s.many(n_std, "STANDARD")
    for _ in range(n_resched):
        s.resched(s.parcel())
    s.complaint(delivered[0])
    return s.pay()


def test_penalty_cap_below_equal_and_above():
    below = _capped(14, 3)           # gross 404, cap 101.00, penalty 100
    assert (below.gross_earnings, below.penalty_cap, below.penalties_uncapped, below.penalties_deducted) == \
        (D2(404), D2("101.00"), D2(100), D2(100))
    equal = _capped(16, 0)           # gross 400, cap 100.00, penalty 100
    assert (equal.gross_earnings, equal.penalty_cap, equal.penalties_deducted) == (D2(400), D2("100.00"), D2(100))
    above = _capped(0, 22, n_resched=2)   # gross 396, cap 99.00, penalty 100
    assert (above.gross_earnings, above.penalty_cap, above.penalties_uncapped, above.penalties_deducted) == \
        (D2(396), D2("99.00"), D2(100), D2("99.00"))
    assert dp.CODE_PENALTY_CAP_RELIEF in codes(above) and dp.CODE_PENALTY_CAP_RELIEF not in codes(equal)
    assert above.net_payout == D2(396) - D2("99.00")


def test_cap_is_applied_once_to_the_combined_penalties():
    s = Scn()
    for _ in range(2):
        s.delivered(s.parcel())                                   # gross 36 + 50 first-attempt = 86
    s.failed(s.parcel(), "RIDER_FAULT")                           # 3 attempts denominators: 2/3 -> no bonus
    late = s.parcel(window_end="08:00:00")
    s.delivered(late, ts="09:00:00")
    s.complaint(late)
    r = s.pay()
    assert r.penalties_uncapped == D2(50 + 20 + 100)
    assert r.penalty_cap == (r.gross_earnings * D2("0.25")).quantize(D2("0.01"), rounding=ROUND_DOWN)
    assert r.penalties_deducted == r.penalty_cap < r.penalties_uncapped      # one combined deduction
    assert r.total_deductions == r.penalties_deducted


def test_cap_rounds_down_to_paise_so_it_never_exceeds_25_percent():
    s = Scn()
    s.odometer("1000.00", "1000.03")
    s.complaint(s.many(1)[0])
    r = s.pay(plain_route(0.03))        # gross = 18 + 0.11 + 50 bonus
    assert r.gross_earnings == D2("68.11")
    assert r.penalty_cap == D2("17.02") and r.penalty_cap <= r.gross_earnings * D2("0.25")


# --------------------------------------------------------------------------
# BR-09 COD
# --------------------------------------------------------------------------


def cod_day(amounts=("500.00", "1500.00")):
    s = Scn()
    for a in amounts:
        s.delivered(s.parcel(cod=a), cod=a)
    return s


def test_no_cod_means_no_shortfall_and_no_cod_warnings():
    s = Scn()
    s.many(2)
    r = s.pay()
    assert (r.cod_collected, r.cod_shortfall, r.cod_surplus) == (D2(0), D2(0), D2(0))
    assert r.deposit_on_time is None and not r.weekly_freeze_flag
    assert dp.WARNING_NO_DEPOSIT not in r.warning_codes


def test_cod_fully_deposited_on_time_reconciles():
    s = cod_day()
    s.deposit("2000.00", ts="18:00:00")
    r = s.pay()
    assert (r.cod_collected, r.cod_deposited_total, r.cod_shortfall) == (D2(2000), D2(2000), D2(0))
    assert r.deposit_on_time is True and r.last_deposit_ts == p_timestamp(f"{D} 18:00:00")
    assert not r.weekly_freeze_flag and dp.CODE_COD_SHORTFALL not in codes(r)


def test_cod_partial_deposit_deducts_shortfall():
    s = cod_day()
    s.deposit("1200.50")
    r = s.pay()
    assert r.cod_shortfall == D2("799.50") and r.total_deductions == D2("799.50")
    assert r.net_payout == r.gross_earnings - D2("799.50")
    assert dp.CODE_COD_SHORTFALL in codes(r, dp.DEDUCTION)


def test_cod_missing_deposit_is_a_full_shortfall_and_reported():
    r = cod_day().pay()
    assert r.cod_shortfall == D2(2000) and dp.WARNING_NO_DEPOSIT in r.warning_codes
    assert r.deposit_on_time is None and r.deposit_count == 0


def test_cod_surplus_is_never_a_negative_deduction_or_credit():
    s = cod_day()
    s.deposit("2300.00")
    r = s.pay()
    assert r.cod_shortfall == D2(0) and r.cod_surplus == D2(300)
    assert r.total_deductions == D2(0) and r.net_payout == r.gross_earnings
    assert dp.WARNING_COD_SURPLUS in r.warning_codes and not r.weekly_freeze_flag


def test_multiple_deposits_are_summed():
    s = cod_day()
    s.deposit("1000.00", ts="15:00:00")
    s.deposit("500.00", ts="16:00:00")
    r = s.pay()
    assert r.cod_deposited_total == D2(1500) and r.cod_shortfall == D2(500) and r.deposit_count == 2


@pytest.mark.parametrize("ts,on_time", [("20:59:59", True), ("21:00:00", True), ("21:00:01", False),
                                        ("21:04:00", False)])
def test_deposit_deadline_boundary(ts, on_time):
    s = cod_day()
    s.deposit("2000.00", ts=ts)
    r = s.pay()
    assert r.deposit_on_time is on_time
    assert (dp.WARNING_LATE_DEPOSIT in r.warning_codes) is (not on_time)
    assert r.cod_deposited_on_time == (D2(2000) if on_time else D2(0))
    assert r.cod_deposited_late == (D2(0) if on_time else D2(2000))


def test_late_deposit_default_counts_cash_but_flags_it():
    s = cod_day()
    s.deposit("2000.00", ts="21:04:00")
    r = s.pay()
    assert r.cod_shortfall == D2(0) and r.cod_credited == D2(2000) and r.deposit_on_time is False


def test_late_deposit_exclude_policy_leaves_amount_in_shortfall():
    s = cod_day()
    s.deposit("1500.00", ts="20:00:00")
    s.deposit("500.00", ts="21:04:00")
    r = s.pay(policy=PayoutPolicy(late_deposit_treatment=LATE_EXCLUDE))
    assert r.cod_credited == D2(1500) and r.cod_shortfall == D2(500) and r.deposit_on_time is False
    with pytest.raises(ValueError):
        PayoutPolicy(late_deposit_treatment="IGNORE")

@pytest.mark.parametrize(
    "field,value",
    [
        ("reschedule_counts_as_first_attempt_miss", "false"),
        ("reschedule_counts_as_first_attempt_miss", 1),
        ("reschedule_counts_as_first_attempt_miss", None),
        ("pay_upgraded_priority_as_express", "false"),
        ("pay_upgraded_priority_as_express", 1),
        ("pay_upgraded_priority_as_express", None),
    ],
)
def test_payout_policy_rejects_non_boolean_flags(field, value):
    with pytest.raises(TypeError):
        PayoutPolicy(**{field: value})
@pytest.mark.parametrize("deposit,shortfall,freeze", [
    ("3000.00", "2000.00", False),     # exactly 2,000: deducted, no freeze
    ("2999.99", "2000.01", True),      # above 2,000: freeze
    ("2000.00", "3000.00", True),
])
def test_freeze_threshold_is_strictly_greater_than_2000(deposit, shortfall, freeze):
    s = cod_day(("2000.00", "3000.00"))
    s.deposit(deposit)
    r = s.pay()
    assert r.cod_shortfall == D2(shortfall) and r.weekly_freeze_flag is freeze
    assert (dp.WARNING_WEEKLY_FREEZE in r.warning_codes) is freeze


def test_cod_counts_only_delivered_events():
    s = Scn()
    s.delivered(s.parcel(cod="700.00"), cod="700.00")
    s.failed(s.parcel(cod="900.00"), "NOT_HOME")
    s.resched(s.parcel(cod="400.00"))
    s.deposit("700.00")
    r = s.pay()
    assert r.cod_collected == D2(700) and r.cod_shortfall == D2(0)


def test_deposits_of_other_riders_and_dates_are_not_used():
    s = cod_day()
    s.deposit("2000.00", rider="RDR-02")
    s.deposit("2000.00", day=D0)
    assert s.pay().cod_shortfall == D2(2000)


def test_negative_net_is_reported_not_hidden():
    s = cod_day(("5000.00",))
    r = s.pay()
    assert r.net_payout < 0 and dp.WARNING_NEGATIVE_NET in r.warning_codes


# --------------------------------------------------------------------------
# Missing data / zero events / argument checks
# --------------------------------------------------------------------------


def test_zero_event_rider_is_explicit_and_safe():
    s = Scn()
    r = s.pay()
    assert r.delivered_count == 0 and r.gross_earnings == D2(0) and r.net_payout == D2(0)
    assert dp.WARNING_NO_EVENTS in r.warning_codes and r.status == dp.STATUS_REVIEW
    assert r.line_items == r.warnings


def test_zero_event_rider_with_derived_route_and_odometer():
    s = Scn()
    s.odometer("1000.00", "1010.00")
    r = calculate_daily_payout(s.ctx(), "RDR-01", D)
    assert r.planned_distance_km == D2("0.00") and r.regular_distance_pay == D2(0)
    assert r.odometer_discrepancy_flag and dp.WARNING_NO_EVENTS in r.warning_codes


def test_derived_route_is_none_without_a_roster_shift():
    s = Scn()
    s.many(2, rider="RDR-02")
    ctx = s.ctx()
    assert derive_planned_route(ctx, "RDR-02", D) is None
    r = calculate_daily_payout(ctx, "RDR-02", D)
    assert dp.WARNING_NO_PLANNED_ROUTE in r.warning_codes and r.delivered_count == 2


def test_events_outside_the_shift_are_still_paid():
    s = Scn()                                                    # shift 09:00-18:00
    s.delivered(s.parcel(), ts="05:30:00")
    s.delivered(s.parcel(), ts="22:30:00")
    assert s.pay().delivered_count == 2


def test_bad_arguments():
    ctx = Scn().ctx()
    with pytest.raises(ValueError):
        calculate_daily_payout(ctx, "RDR-99", D)
    with pytest.raises(TypeError):
        calculate_daily_payout(ctx, "RDR-01", "2026-10-12")
    with pytest.raises(TypeError):
        calculate_daily_payout(ctx, "RDR-01", datetime(2026, 10, 12, 9, 0))
    with pytest.raises(TypeError):
        calculate_daily_payout(ctx, "RDR-01", D, planned_route=20.0)


# --------------------------------------------------------------------------
# Totals, audit trail, precision, determinism
# --------------------------------------------------------------------------


def full_day() -> DailyPayoutResult:
    s = Scn()
    s.many(31, "STANDARD")
    s.many(2, "EXPRESS")
    s.failed(s.parcel(), "NOT_HOME")
    s.failed(s.parcel(), "RIDER_FAULT")
    s.resched(s.parcel())
    late = s.parcel(window_end="09:00:00", cod="1000.00")
    s.delivered(late, ts="09:00:01", cod="1000.00")
    s.complaint(late)
    s.odometer("1000.00", "1018.00")
    s.deposit("400.00", ts="21:10:00")
    return s.pay(_overtime_route(), policy=PayoutPolicy(late_deposit_treatment=LATE_EXCLUDE))


def test_gross_net_and_line_items_add_up():
    r = full_day()
    assert r.gross_earnings == (r.delivery_pay + r.failed_attempt_pay + r.regular_distance_pay
                                + r.overtime_distance_pay + r.volume_incentive + r.first_attempt_incentive)
    assert r.net_payout == r.gross_earnings - r.penalties_deducted - r.cod_shortfall
    assert r.total_deductions == r.penalties_deducted + r.cod_shortfall
    assert sum(i.amount for i in r.line_items if i.kind in dp.NET_KINDS) == r.net_payout
    assert sum(i.amount for i in r.line_items if i.kind in (dp.EARNING,)) == r.gross_earnings
    assert all(i.amount == 0 for i in r.line_items if i.kind in (dp.INFO, dp.WARNING))
    assert r.volume_incentive == D2(100) and r.penalties_uncapped == D2(50 + 20 + 100)
    assert r.cod_deposited_late == D2(400) and r.cod_credited == D2(0)       # EXCLUDE policy
    assert r.cod_shortfall == D2(1000) and not r.weekly_freeze_flag


def test_no_float_anywhere_in_a_result():
    r = full_day()
    for f in fields(r):
        v = getattr(r, f.name)
        assert not isinstance(v, float), f.name
    for i in r.line_items:
        assert not any(isinstance(x, float) for x in (i.amount, i.quantity, i.unit_amount))


def test_results_are_deterministic_and_immutable():
    a, b = full_day(), full_day()
    assert a == b
    with pytest.raises(Exception):
        a.net_payout = D2(1)                                    # frozen dataclass


def test_csv_row_uses_readme_columns_and_formats():
    r = full_day()
    row = r.to_csv_row()
    assert list(row) == ["date", "rider_id", "delivered", "failed_paid", "parcel_pay", "planned_km", "actual_km",
                         "km_used", "odometer_flag", "distance_pay", "overtime_pay", "incentive",
                         "first_attempt_bonus", "gross", "penalties_raw", "penalties_capped", "cod_shortfall", "net"]
    assert row["date"] == "2026-10-12" and row["odometer_flag"] == "false" and row["failed_paid"] == "1"
    assert row["planned_km"] == "20.00" and row["km_used"] == "18.00" and row["net"] == f"{r.net_payout:.2f}"
    none_route = Scn().pay(None).to_csv_row()
    assert none_route["planned_km"] == "" and none_route["actual_km"] == ""


# --------------------------------------------------------------------------
# Batch API
# --------------------------------------------------------------------------


def test_batch_covers_riders_who_worked_including_off_roster_event_riders():
    s = Scn()
    s.many(2, rider="RDR-01")
    s.many(1, rider="RDR-02")                    # no roster shift, but has events: still paid
    ctx = s.ctx()
    assert riders_for_date(ctx, D) == ("RDR-01", "RDR-02")
    out = calculate_daily_payouts(ctx, D)
    assert [r.rider_id for r in out] == ["RDR-01", "RDR-02"]
    assert [r.delivered_count for r in out] == [2, 1]
    assert riders_for_date(ctx, D0) == ()
    assert calculate_daily_payouts(ctx, D, rider_ids=["RDR-02"])[0].rider_id == "RDR-02"


def test_batch_planned_routes_mapping_is_authoritative():
    s = Scn()
    s.many(1, rider="RDR-01")
    s.many(1, rider="RDR-02")
    s.odometer("1000.00", "1020.00")
    out = calculate_daily_payouts(s.ctx(), D, planned_routes={"RDR-01": plain_route(20)})
    by = {r.rider_id: r for r in out}
    assert by["RDR-01"].planned_distance_km == D2("20.00")
    assert by["RDR-02"].planned_distance_km is None and dp.WARNING_NO_PLANNED_ROUTE in by["RDR-02"].warning_codes


def test_plan_result_routes_can_be_passed_straight_in():
    from ps035.assignment import assign_parcels
    s = Scn()
    pid = s.parcel(window_end="23:00:00")
    s.delivered(pid, ts="10:00:00")
    ctx = s.ctx()
    plan = assign_parcels(RIDERS, [HUB], s.parcels, s.shifts, D)
    routes = {a.rider_id: a.route for a in plan.assignments}
    r = calculate_daily_payouts(ctx, D, planned_routes=routes)[0]
    assert r.planned_distance_km == Decimal(repr(routes["RDR-01"].total_km)).quantize(D2("0.01"))
