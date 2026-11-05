"""Weekly payout checked against the official sample pack in data/ (never modified)."""
from __future__ import annotations

import copy
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ps035 import weekly_payout as wp
from ps035.daily_payout import PayoutContext, calculate_daily_payouts
from ps035.loader import load_dataset
from ps035.weekly_payout import (
    HOLD_SHORTFALL, RULE_DELIVERED_ONLY, WeeklyPolicy, calculate_weekly_payout, calculate_weekly_payouts,
)

DATA = Path(__file__).resolve().parent.parent / "data"
pytestmark = pytest.mark.skipif(not (DATA / "parcels.csv").is_file(), reason="sample data not present")

D = Decimal
MON = date(2026, 10, 12)
WEEK = [MON + timedelta(days=i) for i in range(6)]


@pytest.fixture(scope="module")
def ctx():
    return PayoutContext.from_dataset(load_dataset(DATA))


@pytest.fixture(scope="module")
def dailies(ctx):
    return tuple(r for d in WEEK for r in calculate_daily_payouts(ctx, d))


@pytest.fixture(scope="module")
def weekly(ctx, dailies):
    return {w.rider_id: w for w in calculate_weekly_payouts(ctx, MON, daily_results=dailies)}


def test_every_sample_rider_gets_one_weekly_statement(weekly, ctx):
    assert sorted(weekly) == sorted(ctx.riders) and len(weekly) == 8
    assert all(w.week_start == MON and w.week_end == date(2026, 10, 17) for w in weekly.values())


def test_weekly_matches_what_the_daily_api_would_produce_by_itself(ctx, weekly):
    """Omitting daily_results makes the weekly module call the existing daily API: same numbers."""
    for rid in ("RDR-01", "RDR-04", "RDR-08"):
        assert calculate_weekly_payout(ctx, rid, MON) == weekly[rid]


def test_daily_net_total_is_the_plain_sum_of_the_daily_results(weekly, dailies):
    for rid, w in weekly.items():
        mine = [d for d in dailies if d.rider_id == rid]
        assert len(mine) == 6
        assert w.daily_net_total == sum((d.net_payout for d in mine), D(0))
        assert w.daily_gross_total == sum((d.gross_earnings for d in mine), D(0))
        assert w.cod_shortfall_total == sum((d.cod_shortfall for d in mine), D(0))


def test_all_sample_riders_have_six_working_days_and_500_bonus(weekly):
    assert {w.working_days for w in weekly.values()} == {6}
    assert {w.attendance_bonus for w in weekly.values()} == {D("500")}


def test_ev_allowance_only_for_ev_bike_riders(weekly, ctx):
    for rid, w in weekly.items():
        if ctx.riders[rid].vehicle_type == "EV_BIKE":
            assert w.ev_allowance == D("180")
        else:
            assert w.ev_allowance == D("0")
    assert {rid for rid, w in weekly.items() if w.ev_allowance} == {"RDR-03", "RDR-04", "RDR-05"}


def test_EC08_rdr04_failed_only_saturday_counts_by_default(weekly):
    w = weekly["RDR-04"]
    sat = w.daily_lines[-1]
    assert sat.day == date(2026, 10, 17) and sat.delivered_count == 0 and sat.outcome_event_count == 1
    assert sat.qualifies and w.working_days == 6 and w.attendance_bonus == D("500")
    assert wp.WARNING_NO_DELIVERY_DAY in w.warning_codes


def test_EC08_alternative_rule_drops_the_bonus_and_one_ev_day(ctx, dailies, weekly):
    w = calculate_weekly_payout(ctx, "RDR-04", MON, daily_results=dailies,
                                policy=WeeklyPolicy(working_day_rule=RULE_DELIVERED_ONLY))
    assert w.working_days == 5 and w.attendance_bonus == D("0") and w.ev_allowance == D("150")
    assert w.weekly_gross == weekly["RDR-04"].weekly_gross - D("500") - D("30")
    others = calculate_weekly_payouts(ctx, MON, daily_results=dailies,
                                      policy=WeeklyPolicy(working_day_rule=RULE_DELIVERED_ONLY))
    assert [o.working_days for o in others if o.rider_id != "RDR-04"] == [6] * 7   # nobody else is affected


def test_EC07_rdr01_cod_exactly_2000_short_is_deducted_daily_but_not_frozen(weekly, dailies):
    day = next(d for d in dailies if d.rider_id == "RDR-01" and d.operating_date == date(2026, 10, 14))
    assert day.cod_shortfall == D("2000.00") and not day.weekly_freeze_flag
    w = weekly["RDR-01"]
    assert not w.freeze_flag and w.freeze_dates == () and w.amount_held == D("0")
    assert w.amount_payable == w.net_rounded
    assert not any(i.code.startswith("COD") for i in w.line_items)       # nothing deducted a second time


def test_rdr08_cod_2600_short_on_monday_freezes_the_week(weekly, dailies):
    w = weekly["RDR-08"]
    assert w.freeze_flag and w.freeze_dates == (MON,) and w.max_daily_cod_shortfall == D("2600.00")
    assert w.to_csv_row()["frozen"] == "true"
    assert w.net_rounded > 0 and w.amount_held == w.net_rounded and w.amount_payable == D("0")
    assert sum(1 for x in weekly.values() if x.freeze_flag) == 1


def test_rdr08_hold_shortfall_policy_pays_the_remaining_balance(ctx, dailies, weekly):
    w = calculate_weekly_payout(ctx, "RDR-08", MON, daily_results=dailies,
                                policy=WeeklyPolicy(freeze_hold_mode=HOLD_SHORTFALL))
    assert w.freeze_flag and w.amount_held == weekly["RDR-08"].net_rounded      # net < shortfall: everything held
    assert w.amount_payable == D("0")


def test_tds_applies_only_above_5000_and_matches_one_percent(weekly):
    for w in weekly.values():
        if w.weekly_gross > D("5000"):
            assert w.tds_status == wp.TDS_APPLIED and w.tds_amount == (w.weekly_gross * D("0.01")).quantize(D("0.01"))
        else:
            assert w.tds_status == wp.TDS_NOT_APPLICABLE and w.tds_amount == D("0.00")
    assert {rid for rid, w in weekly.items() if w.tds_amount} == {"RDR-01", "RDR-02"}


def test_net_rounded_is_whole_rupees_and_consistent(weekly):
    for w in weekly.values():
        assert w.net_rounded == w.net_rounded.to_integral_value()
        assert abs(w.net_rounded - (w.weekly_gross - w.tds_amount)) <= D("0.5")
        assert w.weekly_gross == w.daily_net_total + w.attendance_bonus + w.ev_allowance


def test_regression_pinned_sample_values(weekly):
    expect = {   # rider: (daily_net_total, weekly_gross, tds, net_rounded)
        "RDR-01": ("5411.96", "5911.96", "59.12", "5853"),
        "RDR-02": ("7181.54", "7681.54", "76.82", "7605"),
        "RDR-03": ("2466.22", "3146.22", "0.00", "3146"),
        "RDR-04": ("2388.46", "3068.46", "0.00", "3068"),
        "RDR-05": ("2392.46", "3072.46", "0.00", "3072"),
        "RDR-06": ("2283.09", "2783.09", "0.00", "2783"),
        "RDR-07": ("2276.88", "2776.88", "0.00", "2777"),
        "RDR-08": ("-116.70", "383.30", "0.00", "383"),
    }
    got = {rid: (str(w.daily_net_total), str(w.weekly_gross), str(w.tds_amount), str(w.net_rounded))
           for rid, w in weekly.items()}
    assert got == expect


def test_sunday_result_is_outside_the_week(ctx, dailies, weekly):
    from dataclasses import replace
    sunday = date(2026, 10, 18)
    fake = replace(next(d for d in dailies if d.rider_id == "RDR-01"), operating_date=sunday)
    w = calculate_weekly_payout(ctx, "RDR-01", MON, daily_results=list(dailies) + [fake])
    assert w.daily_net_total == weekly["RDR-01"].daily_net_total
    assert w.working_days == 6 and sunday not in w.working_dates
    assert wp.WARNING_OUT_OF_PERIOD in w.warning_codes


def test_weekly_calculation_leaves_daily_results_untouched(ctx, dailies):
    before = copy.deepcopy(dailies)
    calculate_weekly_payouts(ctx, MON, daily_results=dailies)
    assert dailies == before


def test_sample_csv_rows_have_the_readme_columns(weekly):
    cols = ["week_start", "rider_id", "working_days", "daily_net_total", "attendance_bonus", "ev_allowance",
            "weekly_gross", "tds", "net_rounded", "frozen"]
    for w in weekly.values():
        assert list(w.to_csv_row()) == cols


def test_week_with_no_data_is_safe(ctx):
    nxt = MON + timedelta(days=7)
    out = calculate_weekly_payouts(ctx, nxt)
    assert out == ()
    w = calculate_weekly_payout(ctx, "RDR-01", nxt)
    assert w.working_days == 0 and w.net_rounded == D("0") and not w.freeze_flag
    assert wp.WARNING_NO_ACTIVITY in w.warning_codes
