"""Weekly payout (BR-10): focused rule tests on hand-built DailyPayoutResult objects.

The daily results are built directly so every weekly rule is tested on its own;
the official sample pack is checked in tests/test_weekly_payout_sample.py.
"""
from __future__ import annotations

import copy
from dataclasses import fields, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from ps035 import weekly_payout as wp
from ps035.daily_payout import DailyPayoutResult, PayoutContext
from ps035.loader import Rider, RiderShift, load_dataset
from ps035.weekly_payout import (
    HOLD_ALL, HOLD_SHORTFALL, RULE_DELIVERED_ONLY, RULE_OUTCOME_EVENT, RULE_ROSTERED, WeeklyPolicy,
    calculate_weekly_payout, calculate_weekly_payouts, compute_tds, week_dates,
)

D = Decimal
MON = date(2026, 10, 12)
WEEK = [MON + timedelta(days=i) for i in range(6)]      # Mon..Sat
SUN = MON + timedelta(days=6)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def daily(rider="R1", day=MON, *, net="100", gross=None, delivered=1, failed_paid=0, failed_unpaid=0,
          rescheduled=0, shortfall="0", freeze=None, status=None) -> DailyPayoutResult:
    """A real DailyPayoutResult with every field filled; only the weekly-relevant ones matter."""
    net_d, short = D(net), D(shortfall)
    gross_d = D(gross) if gross is not None else net_d + short
    values = {f.name: None for f in fields(DailyPayoutResult)}
    zero_ints = ("delivered_count", "standard_delivered_count", "express_delivered_count", "failed_paid_count",
                 "failed_unpaid_count", "rescheduled_count", "first_attempt_successes", "eligible_attempt_count",
                 "rider_fault_count", "late_delivery_count", "complaint_count", "deposit_count")
    for name in zero_ints:
        values[name] = 0
    for f in fields(DailyPayoutResult):
        if f.type in ("Decimal", Decimal):
            values[f.name] = D(0)
    values.update(
        rider_id=rider, operating_date=day, delivered_count=delivered, failed_paid_count=failed_paid,
        failed_unpaid_count=failed_unpaid, rescheduled_count=rescheduled, distance_source="NONE",
        odometer_discrepancy_flag=False, gross_earnings=gross_d, net_payout=net_d, cod_shortfall=short,
        weekly_freeze_flag=(short > 2000) if freeze is None else freeze,
        status=status or "OK", line_items=(), warnings=(), cod_collected=short, cod_credited=D(0),
    )
    return DailyPayoutResult(**values)


@pytest.fixture
def ctx(data_dir):
    """Context with two synthetic riders (R1 = BIKE, EV1 = EV_BIKE), both rostered Mon..Sat; R3 rostered Mon..Fri."""
    real = PayoutContext.from_dataset(load_dataset(data_dir))
    def rider(rid, vt):
        return Rider(rid, rid, vt, 40, D("25.0"), "HUB-01", time(9), time(18))
    riders = {"R1": rider("R1", "BIKE"), "EV1": rider("EV1", "EV_BIKE"), "R3": rider("R3", "BIKE")}
    shifts = {}
    for rid, days in (("R1", WEEK), ("EV1", WEEK), ("R3", WEEK[:5])):
        for d in days:
            shifts[(rid, d)] = RiderShift(rid, d, time(9), time(18))
    return replace(real, riders=riders, shifts=shifts)


def full_week(rider="R1", **kw):
    return [daily(rider, d, **kw) for d in WEEK]


# --------------------------------------------------------------------------
# A. Weekly period
# --------------------------------------------------------------------------


def test_week_is_monday_to_saturday_and_excludes_sunday():
    days = week_dates(MON)
    assert days[0].weekday() == 0 and days[-1].weekday() == 5
    assert len(days) == 6 and SUN not in days
    assert days == tuple(WEEK)


@pytest.mark.parametrize("bad", [MON + timedelta(days=1), SUN, MON - timedelta(days=1)])
def test_week_start_must_be_a_monday(ctx, bad):
    with pytest.raises(ValueError, match="Monday"):
        calculate_weekly_payout(ctx, "R1", bad, daily_results=[])


def test_week_start_must_be_a_date(ctx):
    with pytest.raises(TypeError):
        calculate_weekly_payout(ctx, "R1", "2026-10-12", daily_results=[])
    with pytest.raises(TypeError):
        calculate_weekly_payout(ctx, "R1", datetime(2026, 10, 12), daily_results=[])


def test_statement_has_explicit_week_boundaries(ctx):
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=full_week())
    assert (r.week_start, r.week_end) == (date(2026, 10, 12), date(2026, 10, 17))
    assert [line.day for line in r.daily_lines] == WEEK


def test_sunday_result_is_not_aggregated_and_not_a_working_day(ctx):
    results = full_week(net="100") + [daily("R1", SUN, net="999")]
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=results)
    assert r.working_days == 6 and SUN not in r.working_dates
    assert r.daily_net_total == D("600")                      # the Sunday 999 is excluded
    assert wp.WARNING_OUT_OF_PERIOD in r.warning_codes


def test_result_of_the_neighbouring_weeks_is_not_aggregated(ctx):
    results = full_week() + [daily("R1", MON - timedelta(days=2)), daily("R1", MON + timedelta(days=7))]
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=results)
    assert r.daily_net_total == D("600")
    assert r.warning_codes.count(wp.WARNING_OUT_OF_PERIOD) == 2


def test_monday_and_saturday_are_both_inside(ctx):
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=[daily("R1", MON), daily("R1", WEEK[-1])])
    assert r.working_dates == (MON, WEEK[-1])


def test_other_riders_results_are_ignored(ctx):
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=full_week() + full_week("EV1", net="500"))
    assert r.daily_net_total == D("600")


def test_unknown_rider_raises(ctx):
    with pytest.raises(ValueError, match="unknown rider"):
        calculate_weekly_payout(ctx, "NOPE", MON, daily_results=[])


# --------------------------------------------------------------------------
# B. Attendance bonus and the working-day definition
# --------------------------------------------------------------------------


def test_six_qualifying_days_earn_500(ctx):
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=full_week())
    assert r.working_days == 6 and r.attendance_bonus == D("500")


@pytest.mark.parametrize("n", [0, 1, 4, 5])
def test_fewer_than_six_days_earn_no_attendance_bonus(ctx, n):
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=[daily("R1", d) for d in WEEK[:n]])
    assert r.working_days == n and r.attendance_bonus == D("0")


def test_rostered_day_without_a_daily_result_is_reported_and_not_counted(ctx):
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=full_week()[:5])    # Saturday missing
    assert r.working_days == 5 and r.attendance_bonus == D("0")
    assert wp.WARNING_MISSING_DAILY_RESULT in r.warning_codes and r.status == wp.STATUS_REVIEW
    sat = r.daily_lines[-1]
    assert not sat.has_daily_result and not sat.qualifies and sat.net_payout == D("0")


def test_day_off_without_a_roster_row_is_not_a_warning(ctx):
    r = calculate_weekly_payout(ctx, "R3", MON, daily_results=[daily("R3", d) for d in WEEK[:5]])
    assert r.working_days == 5 and r.warnings == ()           # Saturday is simply not rostered


def test_zero_event_day_is_not_a_working_day_by_default(ctx):
    results = full_week()
    results[2] = daily("R1", WEEK[2], delivered=0, net="0")   # rostered, result exists, no events
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=results)
    assert r.working_days == 5 and WEEK[2] not in r.working_dates and r.attendance_bonus == D("0")


def test_complaint_only_day_is_not_a_working_day(ctx):
    results = full_week()
    results[0] = daily("R1", MON, delivered=0, net="-100")    # outcome counts are 0 (complaints are not attempts)
    assert calculate_weekly_payout(ctx, "R1", MON, daily_results=results).working_days == 5


def test_failed_only_day_counts_by_default_EC08(ctx):
    """EC-08: a day with only a FAILED NOT_HOME attempt is a working day under the documented default."""
    results = full_week()
    results[5] = daily("R1", WEEK[5], delivered=0, failed_paid=1, net="10")
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=results)
    assert r.working_days == 6 and r.attendance_bonus == D("500")
    assert WEEK[5] in r.working_dates
    nd = [w for w in r.warnings if w.code == wp.WARNING_NO_DELIVERY_DAY]
    assert len(nd) == 1 and "EC-08" in nd[0].description and "counted" in nd[0].description
    assert any("FAILED-only day counts" in a for a in r.assumptions)


def test_failed_only_day_can_be_excluded_by_policy_EC08(ctx):
    results = full_week()
    results[5] = daily("R1", WEEK[5], delivered=0, failed_paid=1, net="10")
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=results,
                                policy=WeeklyPolicy(working_day_rule=RULE_DELIVERED_ONLY))
    assert r.working_days == 5 and r.attendance_bonus == D("0")
    assert WEEK[5] not in r.working_dates
    assert "NOT counted" in [w for w in r.warnings if w.code == wp.WARNING_NO_DELIVERY_DAY][0].description
    assert r.daily_net_total == D("510")                       # the day is still PAID through the daily net


def test_failed_unpaid_and_rescheduled_only_days_follow_the_same_default(ctx):
    for kw in ({"failed_unpaid": 1}, {"rescheduled": 1}):
        results = full_week()
        results[1] = daily("R1", WEEK[1], delivered=0, net="0", **kw)
        assert calculate_weekly_payout(ctx, "R1", MON, daily_results=results).working_days == 6


def test_rostered_rule_counts_zero_event_days(ctx):
    results = [daily("R1", d, delivered=0, net="0") for d in WEEK]
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=results, policy=WeeklyPolicy(working_day_rule=RULE_ROSTERED))
    assert r.working_days == 6 and r.attendance_bonus == D("500")


def test_off_roster_events_are_paid_but_not_a_working_day_by_default(ctx):
    results = [daily("R3", d) for d in WEEK[:5]] + [daily("R3", WEEK[5], net="70")]      # R3 is not rostered Saturday
    r = calculate_weekly_payout(ctx, "R3", MON, daily_results=results)
    assert r.working_days == 5 and r.attendance_bonus == D("0")
    assert r.daily_net_total == D("570")                       # still paid (daily net is authoritative)
    assert wp.WARNING_OFF_ROSTER_WORK in r.warning_codes
    r2 = calculate_weekly_payout(ctx, "R3", MON, daily_results=results, policy=WeeklyPolicy(require_roster_shift=False))
    assert r2.working_days == 6 and r2.attendance_bonus == D("500")


def test_policy_values_are_validated():
    with pytest.raises(ValueError):
        WeeklyPolicy(working_day_rule="SOMETIMES")
    with pytest.raises(ValueError):
        WeeklyPolicy(freeze_hold_mode="HALF")


# --------------------------------------------------------------------------
# C. EV allowance
# --------------------------------------------------------------------------


def test_ev_bike_gets_30_per_qualifying_day(ctx):
    for n in (0, 1, 3, 6):
        r = calculate_weekly_payout(ctx, "EV1", MON, daily_results=[daily("EV1", d) for d in WEEK[:n]])
        assert r.vehicle_type == "EV_BIKE" and r.ev_allowance == D(30) * n


def test_non_ev_rider_gets_no_ev_allowance(ctx):
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=full_week())
    assert r.ev_allowance == D("0")


def test_ev_allowance_uses_the_same_working_day_definition(ctx):
    results = [daily("EV1", d) for d in WEEK]
    results[5] = daily("EV1", WEEK[5], delivered=0, failed_paid=1, net="10")
    default = calculate_weekly_payout(ctx, "EV1", MON, daily_results=results)
    strict = calculate_weekly_payout(ctx, "EV1", MON, daily_results=results,
                                     policy=WeeklyPolicy(working_day_rule=RULE_DELIVERED_ONLY))
    assert (default.working_days, default.ev_allowance) == (6, D("180"))
    assert (strict.working_days, strict.ev_allowance) == (5, D("150"))


# --------------------------------------------------------------------------
# D. TDS
# --------------------------------------------------------------------------


def _gross_week(ctx, weekly_gross, rider="R1"):
    """One daily result whose net IS the wanted weekly gross (no bonuses: 1 day, non-EV)."""
    return calculate_weekly_payout(ctx, rider, MON, daily_results=[daily(rider, MON, net=weekly_gross)])


def test_weekly_gross_exactly_5000_has_no_tds(ctx):
    r = _gross_week(ctx, "5000.00")
    assert r.weekly_gross == D("5000.00") and r.tds_amount == D("0.00") and r.tds_status == wp.TDS_NOT_APPLICABLE
    assert r.net_rounded == D("5000")


def test_weekly_gross_just_above_5000_has_tds(ctx):
    r = _gross_week(ctx, "5000.01")
    assert r.tds_status == wp.TDS_APPLIED and r.tds_amount == D("50.00")     # 50.0001 -> 50.00
    assert r.net_before_rounding == D("4950.01") and r.net_rounded == D("4950")


def test_weekly_gross_just_below_5000_has_no_tds(ctx):
    assert _gross_week(ctx, "4999.99").tds_amount == D("0.00")


def test_tds_base_is_weekly_gross_including_bonuses(ctx):
    """Daily net 4,500 + attendance 500 + EV 180 = 5,180 -> TDS applies because of the weekly items."""
    results = [daily("EV1", d, net="750") for d in WEEK]
    r = calculate_weekly_payout(ctx, "EV1", MON, daily_results=results)
    assert r.daily_net_total == D("4500") and r.attendance_bonus == D("500") and r.ev_allowance == D("180")
    assert r.weekly_gross == r.tds_base == D("5180")
    assert r.tds_amount == D("51.80") and r.net_before_rounding == D("5128.20") and r.net_rounded == D("5128")


def test_tds_base_is_not_the_daily_gross(ctx):
    """Daily gross is above 5,000 but the (post-penalty/COD) weekly gross is not: no TDS."""
    results = [daily("R1", MON, net="4000", gross="6000")]
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=results)
    assert r.daily_gross_total == D("6000") and r.weekly_gross == D("4000") and r.tds_amount == D("0.00")


@pytest.mark.parametrize("gross, tds", [
    ("5000.50", "50.01"),    # 50.005 -> half-up -> 50.01
    ("5000.49", "50.00"),    # 50.0049 -> 50.00
    ("5123.45", "51.23"),    # 51.2345 -> 51.23
    ("10000.00", "100.00"),
])
def test_tds_is_one_percent_rounded_half_up_to_paise(gross, tds):
    assert compute_tds(D(gross)) == D(tds)


@pytest.mark.parametrize("net, expected", [("1000.49", "1000"), ("1000.50", "1001"), ("1000.51", "1001"), ("2776.88", "2777")])
def test_net_is_rounded_half_up_to_the_nearest_rupee(ctx, net, expected):
    r = _gross_week(ctx, net)
    assert r.net_rounded == D(expected) and r.net_rounded == r.net_rounded.to_integral_value()


def test_money_is_decimal_not_float(ctx):
    r = calculate_weekly_payout(ctx, "EV1", MON, daily_results=[daily("EV1", d, net="0.10") for d in WEEK])
    assert r.daily_net_total == D("0.60")                      # 6 x 0.10 exactly (a float sum would be 0.6000000000000001)
    for name in ("daily_net_total", "attendance_bonus", "ev_allowance", "weekly_gross", "tds_amount", "net_rounded",
                 "amount_held", "amount_payable", "cod_shortfall_total"):
        assert isinstance(getattr(r, name), Decimal)


def test_line_items_add_up_to_net_rounded(ctx):
    for gross in ("5000.00", "5180.37", "1000.50"):
        r = _gross_week(ctx, gross)
        signed = sum((i.amount for i in r.line_items if i.kind in ("EARNING", "DEDUCTION", "ADJUSTMENT")), D(0))
        assert signed == r.net_rounded


# --------------------------------------------------------------------------
# E / F. COD freeze, no double deduction
# --------------------------------------------------------------------------


def test_cod_exactly_2000_does_not_freeze(ctx):
    """Reuses the daily flag: shortfall 2000.00 -> weekly_freeze_flag False -> not frozen (EC-07)."""
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=[daily("R1", MON, net="3000", shortfall="2000")])
    assert not r.freeze_flag and r.freeze_dates == () and r.amount_held == D("0")
    assert r.cod_shortfall_total == D("2000")
    assert r.amount_payable == r.net_rounded


def test_cod_above_2000_freezes_and_holds_everything_by_default(ctx):
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=[daily("R1", MON, net="3000", shortfall="2000.01")])
    assert r.freeze_flag and r.freeze_dates == (MON,) and wp.WARNING_WEEKLY_FREEZE in r.warning_codes
    assert r.net_rounded == D("3000") and r.amount_held == D("3000") and r.amount_payable == D("0")
    assert r.to_csv_row()["frozen"] == "true"


def test_freeze_hold_shortfall_mode_holds_only_the_shortfall(ctx):
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=[daily("R1", MON, net="3000", shortfall="2600")],
                                policy=WeeklyPolicy(freeze_hold_mode=HOLD_SHORTFALL))
    assert r.freeze_flag and r.amount_held == D("2600") and r.amount_payable == D("400")
    assert r.amount_held + r.amount_payable == r.net_rounded


def test_hold_is_capped_at_the_net_and_never_negative(ctx):
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=[daily("R1", MON, net="300", shortfall="2600")],
                                policy=WeeklyPolicy(freeze_hold_mode=HOLD_SHORTFALL))
    assert r.amount_held == D("300") and r.amount_payable == D("0")
    neg = calculate_weekly_payout(ctx, "R1", MON, daily_results=[daily("R1", MON, net="-500", shortfall="2600")])
    assert neg.freeze_flag and neg.amount_held == D("0") and neg.amount_payable == D("-500")
    assert wp.WARNING_NEGATIVE_NET in neg.warning_codes


def test_one_frozen_day_freezes_the_whole_week(ctx):
    results = full_week()
    results[3] = daily("R1", WEEK[3], net="100", shortfall="2500")
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=results)
    assert r.freeze_flag and r.freeze_dates == (WEEK[3],)
    assert [line.freeze_flag for line in r.daily_lines] == [False, False, False, True, False, False]


def test_weekly_total_over_2000_without_a_frozen_day_is_only_a_warning_by_default(ctx):
    results = [daily("R1", d, net="500", shortfall="1000") for d in WEEK[:3]]      # 3 x 1000 = 3000, no day > 2000
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=results)
    assert not r.freeze_flag and r.cod_shortfall_total == D("3000")
    assert wp.WARNING_WEEKLY_COD_TOTAL in r.warning_codes
    strict = calculate_weekly_payout(ctx, "R1", MON, daily_results=results, policy=WeeklyPolicy(freeze_on_weekly_total=True))
    assert strict.freeze_flag and strict.freeze_dates == () and strict.amount_held == strict.net_rounded


def test_weekly_total_of_exactly_2000_never_freezes(ctx):
    results = [daily("R1", d, net="500", shortfall="1000") for d in WEEK[:2]]
    for policy in (WeeklyPolicy(), WeeklyPolicy(freeze_on_weekly_total=True)):
        assert not calculate_weekly_payout(ctx, "R1", MON, daily_results=results, policy=policy).freeze_flag


def test_cod_shortfall_and_penalties_are_not_deducted_again(ctx):
    """daily net already contains them: weekly gross is exactly net total + bonuses, and no COD/penalty line exists."""
    results = [daily("EV1", d, net="400", gross="700", shortfall="150") for d in WEEK]
    r = calculate_weekly_payout(ctx, "EV1", MON, daily_results=results)
    assert r.daily_net_total == D("2400")
    assert r.weekly_gross == D("2400") + D("500") + D("180")
    assert r.cod_shortfall_total == D("900")                    # reported only
    codes = {i.code for i in r.line_items}
    assert not any("COD" in c or "PENALTY" in c for c in codes)
    assert r.net_before_rounding == r.weekly_gross - r.tds_amount


def test_no_weekly_penalty_cap_is_applied(ctx):
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=[daily("R1", MON, net="-300", gross="100")])
    assert r.daily_net_total == D("-300") and r.net_rounded == D("-300")


def test_daily_results_are_not_mutated_and_not_recalculated(ctx, monkeypatch):
    results = full_week(net="123.45", shortfall="2100")
    snapshot = copy.deepcopy(results)
    import ps035.weekly_payout as mod

    def boom(*a, **k):
        raise AssertionError("daily payout must not be recalculated when daily_results are supplied")
    monkeypatch.setattr(mod, "calculate_daily_payout", boom)
    monkeypatch.setattr(mod, "calculate_daily_payouts", boom)
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=results)
    calculate_weekly_payouts(ctx, MON, daily_results=results)
    assert results == snapshot
    assert r.daily_net_total == D("123.45") * 6
    with pytest.raises(Exception):
        r.net_rounded = D(0)                                    # the weekly result is frozen too


# --------------------------------------------------------------------------
# Partial weeks, zero-event riders, duplicates
# --------------------------------------------------------------------------


def test_partial_week_aggregates_only_what_exists(ctx):
    r = calculate_weekly_payout(ctx, "EV1", MON, daily_results=[daily("EV1", MON, net="200"), daily("EV1", WEEK[1], net="300")])
    assert r.working_days == 2 and r.daily_net_total == D("500")
    assert r.attendance_bonus == D("0") and r.ev_allowance == D("60") and r.weekly_gross == D("560")
    assert wp.WARNING_MISSING_DAILY_RESULT in r.warning_codes     # four rostered days have no result


def test_zero_event_rider_is_deterministic(ctx):
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=[])
    assert r.working_days == 0 and r.daily_net_total == D("0") and r.weekly_gross == D("0")
    assert r.net_rounded == D("0") and r.amount_payable == D("0") and not r.freeze_flag
    assert r.status == wp.STATUS_REVIEW and r.warning_codes.count(wp.WARNING_MISSING_DAILY_RESULT) == 6
    again = calculate_weekly_payout(ctx, "R1", MON, daily_results=[])
    assert again == r


def test_rider_with_nothing_at_all_is_flagged(ctx, ):
    ctx2 = replace(ctx, shifts={})
    r = calculate_weekly_payout(ctx2, "R1", MON, daily_results=[])
    assert wp.WARNING_NO_ACTIVITY in r.warning_codes and r.net_rounded == D("0")


def test_duplicate_daily_result_is_not_double_counted(ctx):
    results = full_week(net="100") + [daily("R1", MON, net="777")]
    r = calculate_weekly_payout(ctx, "R1", MON, daily_results=results)
    assert r.daily_net_total == D("600") and wp.WARNING_DUPLICATE_DAILY_RESULT in r.warning_codes


def test_result_order_does_not_matter(ctx):
    results = full_week(net="10")
    results[2] = daily("R1", WEEK[2], net="55")
    a = calculate_weekly_payout(ctx, "R1", MON, daily_results=results)
    b = calculate_weekly_payout(ctx, "R1", MON, daily_results=list(reversed(results)))
    assert a == b


def test_batch_returns_sorted_riders_with_rostered_or_resulting_days(ctx):
    results = full_week("EV1") + full_week("R1")
    out = calculate_weekly_payouts(ctx, MON, daily_results=results)
    assert [r.rider_id for r in out] == ["EV1", "R1", "R3"]     # R3 is rostered (no results) and appears with warnings
    only = calculate_weekly_payouts(ctx, MON, daily_results=results, rider_ids=["R1"])
    assert [r.rider_id for r in only] == ["R1"]
    with pytest.raises(ValueError):
        calculate_weekly_payouts(ctx, MON, daily_results=results, rider_ids=["GHOST"])


# --------------------------------------------------------------------------
# Statement content / CSV
# --------------------------------------------------------------------------


def test_statement_contains_every_required_field(ctx):
    results = [daily("EV1", d, net="1000", gross="1100", shortfall="0") for d in WEEK]
    results[1] = daily("EV1", WEEK[1], net="400", gross="1100", shortfall="2600")
    r = calculate_weekly_payout(ctx, "EV1", MON, daily_results=results)
    assert r.rider_id == "EV1" and (r.week_start, r.week_end) == (MON, WEEK[-1])
    assert r.working_days == 6 and r.working_dates == tuple(WEEK)
    assert (r.daily_gross_total, r.daily_net_total) == (D("6600"), D("5400"))
    assert (r.attendance_bonus, r.ev_allowance) == (D("500"), D("180"))
    assert (r.weekly_gross, r.tds_base, r.tds_amount, r.tds_status) == (D("6080"), D("6080"), D("60.80"), wp.TDS_APPLIED)
    assert r.net_rounded == D("6019") and r.freeze_flag and r.amount_held == D("6019") and r.amount_payable == D("0")
    assert r.cod_shortfall_total == D("2600") and r.assumptions
    text = "\n".join(r.statement_lines())
    for needle in ("EV1", "2026-10-12", "2026-10-17", "Working days: 6", "Attendance bonus", "EV allowance",
                   "TDS", "FROZEN", "Amount held", "Amount payable", "ASSUMPTION"):
        assert needle in text


def test_csv_row_follows_the_readme_columns(ctx):
    r = calculate_weekly_payout(ctx, "EV1", MON, daily_results=[daily("EV1", d, net="750.10") for d in WEEK])
    row = r.to_csv_row()
    assert list(row) == ["week_start", "rider_id", "working_days", "daily_net_total", "attendance_bonus",
                         "ev_allowance", "weekly_gross", "tds", "net_rounded", "frozen"]
    assert row == {"week_start": "2026-10-12", "rider_id": "EV1", "working_days": "6", "daily_net_total": "4500.60",
                   "attendance_bonus": "500.00", "ev_allowance": "180.00", "weekly_gross": "5180.60",
                   "tds": "51.81", "net_rounded": "5129.00", "frozen": "false"}
