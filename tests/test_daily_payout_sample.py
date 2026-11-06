"""Daily payout checked against the real official sample pack in data/ (never modified)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pytest

from ps035 import daily_payout as dp
from ps035.assignment import assign_parcels
from ps035.daily_payout import (
    LATE_EXCLUDE, PayoutContext, PayoutPolicy, calculate_daily_payout, calculate_daily_payouts,
)
from ps035.loader import load_dataset

DATA = Path(__file__).resolve().parent.parent / "data"

pytestmark = pytest.mark.skipif(not (DATA / "parcels.csv").is_file(), reason="sample data not present")

WEEK = [date(2026, 10, 12) + timedelta(days=i) for i in range(6)]


@pytest.fixture(scope="module")
def ds():
    return load_dataset(DATA)


@pytest.fixture(scope="module")
def ctx(ds):
    return PayoutContext.from_dataset(ds)


@pytest.fixture(scope="module")
def week(ctx):
    """{(rider_id, date): DailyPayoutResult} for every rider-day of the sample week."""
    return {(r.rider_id, r.operating_date): r for d in WEEK for r in calculate_daily_payouts(ctx, d)}


def day(week, rider, iso):
    return week[(rider, date.fromisoformat(iso))]


def test_every_rider_day_of_the_week_is_calculated(week, ds):
    assert len(week) == 48
    assert {(s.rider_id, s.shift_date) for s in ds.shifts} <= set(week)


def test_all_events_are_used_and_events_outside_shifts_are_not_rejected(week, ds):
    shifts = {(s.rider_id, s.shift_date): s for s in ds.shifts}
    outside = [e for e in ds.events
               if (e.rider_id, e.event_date) not in shifts
               or not (shifts[(e.rider_id, e.event_date)].shift_start <= e.event_ts.time()
                       <= shifts[(e.rider_id, e.event_date)].shift_end)]
    assert len(outside) == 307
    non_complaints = [e for e in ds.events if e.event_type != "COMPLAINT"]
    used = sum(r.delivered_count + r.failed_paid_count + r.failed_unpaid_count + r.rescheduled_count
               for r in week.values())
    assert used == len(non_complaints) == 1527
    assert sum(r.delivered_count for r in week.values()) == 1442
    assert sum(r.complaint_count for r in week.values()) == 3
    assert not any(dp.WARNING_DUPLICATE_EVENT in r.warning_codes for r in week.values())


def test_602_late_deliveries_get_the_20_penalty(week):
    assert sum(r.late_delivery_count for r in week.values()) == 602
    assert all(r.late_delivery_penalty == Decimal(20) * r.late_delivery_count for r in week.values())


def test_pcl_00033_is_late(week):
    r = day(week, "RDR-01", "2026-10-12")
    late = [i for i in r.line_items if i.code == dp.CODE_PENALTY_LATE]
    assert late and "EVT-00018" in late[0].references          # PCL-00033 delivered 13:04:35, window ends 13:00


def test_customer_busy_earns_nothing(week, ds):
    busy = [e for e in ds.events if e.reason_code == "CUSTOMER_BUSY"]
    assert len(busy) == 1
    r = day(week, busy[0].rider_id, str(busy[0].event_date))
    flagged = [w for w in r.warnings if w.code == dp.WARNING_UNRECOGNISED_REASON]
    assert flagged and busy[0].event_id in flagged[0].references
    assert not any(busy[0].event_id in i.references for i in r.line_items if i.code == dp.CODE_FAILED_PAY)
    assert r.failed_unpaid_count >= 1


def test_rdr02_oct16_volume_and_first_attempt_incentives(week):
    r = day(week, "RDR-02", "2026-10-16")
    assert r.delivered_count == 30
    assert (r.first_attempt_successes, r.eligible_attempt_count) == (29, 30)
    assert r.first_attempt_success_rate_pct == Decimal("96.67")
    assert r.volume_incentive == Decimal(100) and r.first_attempt_incentive == Decimal(50)


def test_rdr07_oct17_penalties_are_capped(week):
    r = day(week, "RDR-07", "2026-10-17")
    assert r.delivered_count == 2 and r.rider_fault_count == 2 and r.complaint_count == 1
    assert r.delivery_pay == Decimal(36)                        # README: "~Rs 36"
    assert r.penalties_uncapped == 50 * 2 + 100 + 20 * r.late_delivery_count
    assert r.penalty_cap == (r.gross_earnings * Decimal("0.25")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    assert r.penalties_deducted == r.penalty_cap < r.penalties_uncapped
    assert dp.CODE_PENALTY_CAP_RELIEF in [i.code for i in r.line_items]


def test_cod_exactly_21_00_00_is_on_time_and_21_04_is_late(week, ds):
    on_time = next(d for d in ds.cod_deposits if d.deposit_ts.time().isoformat() == "21:00:00")
    late = next(d for d in ds.cod_deposits if d.deposit_ts.time().isoformat() == "21:04:00")
    assert day(week, on_time.rider_id, str(on_time.deposit_date)).deposit_on_time is True
    r = day(week, late.rider_id, str(late.deposit_date))
    assert r.deposit_on_time is False
    assert late.deposit_id in [w.references[0] for w in r.warnings if w.code == dp.WARNING_LATE_DEPOSIT]


def test_rdr01_oct14_shortfall_exactly_2000_does_not_freeze(week):
    r = day(week, "RDR-01", "2026-10-14")
    assert r.cod_shortfall == Decimal("2000.00") and r.weekly_freeze_flag is False
    assert r.total_deductions == r.penalties_deducted + Decimal("2000.00")


def test_rdr08_oct12_shortfall_2600_sets_the_weekly_freeze_flag(week):
    r = day(week, "RDR-08", "2026-10-12")
    assert r.cod_shortfall == Decimal("2600.00") and r.weekly_freeze_flag is True
    assert {k for k, v in week.items() if v.weekly_freeze_flag} == {("RDR-08", date(2026, 10, 12))}


def test_rdr01_oct13_late_deposit_default_and_exclude_policies(ctx, week):
    r = day(week, "RDR-01", "2026-10-13")
    assert r.deposit_on_time is False and r.cod_deposited_late == r.cod_collected == r.cod_deposited_total
    assert r.cod_shortfall == Decimal(0) and not r.weekly_freeze_flag               # counted, flagged
    strict = calculate_daily_payout(ctx, "RDR-01", date(2026, 10, 13),
                                    policy=PayoutPolicy(late_deposit_treatment=LATE_EXCLUDE))
    assert strict.cod_shortfall == r.cod_collected and strict.weekly_freeze_flag     # not credited


def test_rdr03_oct14_odometer_exactly_10_percent_above_plan_is_accepted(week):
    r = day(week, "RDR-03", "2026-10-14")
    assert r.planned_distance_km == Decimal("13.50") and r.odometer_distance_km == Decimal("14.85")
    assert r.selected_distance_km == Decimal("14.85") and r.odometer_discrepancy_flag is False
    assert dp.WARNING_ODOMETER_DISCREPANCY not in r.warning_codes
    assert r.overtime_distance_km == Decimal(0)          # back at the hub exactly at shift end
    assert r.regular_distance_pay == Decimal("51.98")    # 14.85 x 3.50 = 51.975


def test_rdr04_oct17_failed_only_day_is_paid_for_the_attempt(week):
    r = day(week, "RDR-04", "2026-10-17")
    assert (r.delivered_count, r.failed_paid_count, r.failed_attempt_pay) == (0, 1, Decimal(10))


def test_every_result_is_internally_consistent(week):
    for key, r in week.items():
        assert r.gross_earnings == (r.delivery_pay + r.failed_attempt_pay + r.regular_distance_pay
                                    + r.overtime_distance_pay + r.volume_incentive + r.first_attempt_incentive), key
        assert sum(i.amount for i in r.line_items if i.kind in dp.NET_KINDS) == r.net_payout, key
        assert r.net_payout == r.gross_earnings - r.penalties_deducted - r.cod_shortfall, key
        assert r.penalties_deducted == min(r.penalties_uncapped, r.penalty_cap), key
        assert r.penalties_deducted <= r.gross_earnings * Decimal("0.25"), key
        assert r.cod_shortfall >= 0 and r.cod_surplus >= 0 and not (r.cod_shortfall and r.cod_surplus), key
        assert r.regular_distance_km + r.overtime_distance_km == (r.selected_distance_km or Decimal(0)), key
        assert r.volume_incentive in (0, 100, 250), key
        row = r.to_csv_row()
        assert len(row) == 18 and row["rider_id"] == key[0], key


def test_rate_constants_match_the_brief():
    assert (dp.RATE_STANDARD, dp.RATE_EXPRESS, dp.FAILED_ATTEMPT_PAY) == (Decimal(18), Decimal(25), Decimal(10))
    assert dp.DISTANCE_RATE_PER_KM == Decimal("3.50") and dp.OVERTIME_MULTIPLIER == Decimal("1.5")
    assert (dp.RIDER_FAULT_PENALTY, dp.LATE_DELIVERY_PENALTY, dp.COMPLAINT_PENALTY) == (50, 20, 100)
    assert dp.PENALTY_CAP_RATE == Decimal("0.25") and dp.FREEZE_SHORTFALL_THRESHOLD == Decimal(2000)


def test_sample_week_is_deterministic(ctx, week):
    again = calculate_daily_payouts(ctx, date(2026, 10, 14))
    assert tuple(week[(r.rider_id, r.operating_date)] for r in again) == again


def test_plan_routes_from_the_assignment_module_can_drive_the_payout(ctx, ds):
    d = date(2026, 10, 12)
    pool = ctx.event_log.pool_for_date(d)
    plan = assign_parcels(ds.riders, ds.hubs, pool, ds.shifts, d)
    routes = {a.rider_id: a.route for a in plan.assignments}
    results = calculate_daily_payouts(ctx, d, planned_routes=routes)
    assert len(results) == 8
    for r in results:
        assert r.planned_distance_km == Decimal(repr(routes[r.rider_id].total_km)).quantize(Decimal("0.01"))
        assert r.delivered_count == len([e for e in ds.events if e.rider_id == r.rider_id and e.event_date == d
                                         and e.event_type == "DELIVERED"])
