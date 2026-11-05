"""Weekly payout for ONE rider and ONE Monday-Saturday week (BR-10).

Beginner notes
--------------
* This module only AGGREGATES the existing daily payout results from
  ``ps035.daily_payout`` and then applies the weekly-level items. It never
  recalculates a daily payout itself, never touches penalties or COD shortfalls
  again, never mutates a ``DailyPayoutResult`` and never writes a file.
* Money is ``Decimal`` everywhere (no binary floats).

Public API
----------
``calculate_weekly_payout(ctx, rider_id, week_start, *, daily_results=None, policy=...)``
``calculate_weekly_payouts(ctx, week_start, *, rider_ids=None, daily_results=None, policy=...)``
``week_dates(week_start) -> tuple[date, ...]``        the six Monday..Saturday dates
``WeeklyPolicy``                                       the configurable choices (below)
``WeeklyPayoutResult``                                 the weekly statement

``daily_results`` is an iterable of real ``DailyPayoutResult`` objects (for
example the output of ``calculate_daily_payouts``). If it is omitted, the
existing daily API is called once per day to produce them (routes derived from
events, like the Daily Payout default). Pass PLAN-based results yourself when
you want planned routes from PLAN.

Formula (TDS-base correction: PROJECT WORKING DECISION, not mentor-confirmed)
-------
    daily_gross_total   = sum(gross_earnings of the Mon..Sat daily results)  # BEFORE penalties / COD
    penalties_total     = sum(penalties_deducted of the daily results)        # capped, as applied daily
    cod_shortfall_total = sum(cod_shortfall of the daily results)
    daily_net_total     = sum(net_payout of the daily results)
                        = daily_gross_total - penalties_total - cod_shortfall_total
    attendance_bonus    = 500 if working_days == 6 else 0
    ev_allowance        = 30 * working_days if rider is EV_BIKE else 0
    weekly_gross        = daily_gross_total + attendance_bonus + ev_allowance  # == tds_base
    tds                 = round_half_up_to_paise(1% of weekly_gross) if weekly_gross > 5000 else 0
    net_before_round    = weekly_gross - tds - penalties_total - cod_shortfall_total
                        = daily_net_total + attendance_bonus + ev_allowance - tds
    net_rounded         = round_half_up_to_rupee(net_before_round)             # LAST step
    frozen              = any daily result has weekly_freeze_flag (daily COD shortfall > 2000)
    amount_held / amount_payable = see FREEZE policy below

    ``weekly_gross`` therefore means "gross earnings + weekly bonuses BEFORE any deduction". Daily
    penalties and COD shortfalls do NOT reduce the TDS base; they reduce only the final net, and
    each is deducted exactly once (the code adds the daily NET, which already holds them, to the
    weekly items - it never subtracts them a second time). Because ``weekly_gross`` is no longer
    ``daily_net_total + bonuses``, the weekly_payouts.csv columns keep their README names and order
    but ``weekly_gross`` now carries this meaning.

Weekly period (rule A)
    Monday..Saturday, inclusive: ``week_start`` MUST be a Monday (ValueError
    otherwise) and ``week_end`` is the Saturday (start + 5 days). Sunday is not
    a working day: a daily result dated on a Sunday, or on any other date
    outside the six days, is NOT aggregated and is reported (OUT_OF_PERIOD_RESULT).
    Days with no roster row are days off (no warning). A day that has a roster
    row or a qualifying signal but no supplied daily result is reported
    (MISSING_DAILY_RESULT) and contributes nothing - nothing is invented.

Qualifying working day (rules B and C, ONE definition for both bonuses)
    The project documents leave "working day" open (design doc, EC-08 and the
    open question "Does a day with only a FAILED attempt count as a working
    day?"). ASSUMPTION (needs mentor confirmation), default ``WeeklyPolicy()``:
        a day qualifies when the rider is ROSTERED (rider_shifts.csv row) AND
        has at least one payable outcome event that day (DELIVERED, FAILED or
        RESCHEDULED, as counted by the daily result).
    Why: Daily Payout already treats a FAILED NOT_HOME / WRONG_ADDRESS attempt as
    paid work (Rs 10) and lists "every rider who worked". A COMPLAINT alone is
    not an attempt and does not qualify. Events outside the shift hours do not
    matter (event history is authoritative, as in Daily Payout). Under this
    default EC-08 (RDR-04, 2026-10-17, one FAILED NOT_HOME at 17:27 in a
    17:00-18:00 shift) COUNTS, so RDR-04 gets 6 days -> Rs 500.
    Change it in ONE place: ``WeeklyPolicy.working_day_rule``
        OUTCOME_EVENT (default)  any payable outcome event
        DELIVERED_ONLY           at least one DELIVERED parcel (EC-08 then does NOT count)
        ROSTERED                 a roster row is enough (events ignored)
    and ``WeeklyPolicy.require_roster_shift`` (default True): an off-roster day
    with events is still PAID through the daily net, but is not a working day
    (OFF_ROSTER_WORK reported). Any counted/excluded day with no delivery is
    flagged NO_DELIVERY_DAY so the ambiguity stays visible on the statement.

EV allowance (rule C): Rs 30 x qualifying working days, EV_BIKE riders only.

TDS (rule D)
    BASE = ``weekly_gross`` = daily GROSS earnings total + attendance bonus + EV
    allowance, i.e. BEFORE daily penalties and COD shortfalls (and before TDS).
    Applied only when base is STRICTLY greater than Rs 5,000 (exactly 5,000.00
    -> none). Amount = 1% of the base, rounded HALF_UP to paise. ASSUMPTION: the
    design document only says "1% if weekly gross exceeds 5,000" and lists the TDS
    base and the deduction order as OPEN QUESTIONS ("Is TDS calculated before or
    after incentives and other deductions?"). This base is a PROJECT WORKING
    DECISION taken for this project, NOT a mentor-confirmed rule. (Earlier versions
    used daily NET + bonuses as the base.) Rounding stages: TDS is rounded to paise
    first; the final net is then rounded HALF_UP to the nearest rupee ("Net rounded
    to nearest rupee", BR-10); the rounding difference is shown as its own line item.

COD freeze (rule E)
    Reused from Daily Payout: the week is frozen when ANY included daily result
    has ``weekly_freeze_flag`` (that day's shortfall > 2,000; exactly 2,000 does
    not). The shortfall is ALREADY deducted in the daily net, so it is only
    reported here, never deducted again. Optional
    ``WeeklyPolicy.freeze_on_weekly_total`` (default False) also freezes when the
    week's summed shortfall is > 2,000 even if no single day was; by default such
    a week only gets a WEEKLY_COD_TOTAL_ABOVE_THRESHOLD warning.
    UNRESOLVED POLICY (design doc: "can undisputed earnings be paid?"):
    ``WeeklyPolicy.freeze_hold_mode``
        HOLD_ALL (default)   the whole weekly net (``net_rounded``) is held, payable now = 0
        HOLD_SHORTFALL       only an amount equal to the week's COD shortfall is held
                             (never more than the net); the rest is payable now
    Not frozen: held = 0, payable = net_rounded.
    A negative net is never floored (as in Daily Payout); NEGATIVE_WEEKLY_NET is reported.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

from ps035.daily_payout import (
    ADJUSTMENT, DEDUCTION, EARNING, INFO, WARNING, DailyPayoutResult, LineItem, PayoutContext,
    calculate_daily_payout, calculate_daily_payouts, riders_for_date,
)

# --------------------------------------------------------------------------
# Business-rule constants (all money is Decimal)
# --------------------------------------------------------------------------

PAISE = Decimal("0.01")
RUPEE = Decimal("1")

WEEK_LENGTH_DAYS = 6                              # Monday..Saturday
ATTENDANCE_BONUS = Decimal("500")                 # BR-10
ATTENDANCE_REQUIRED_DAYS = 6
EV_BIKE_VEHICLE_TYPE = "EV_BIKE"
EV_ALLOWANCE_PER_DAY = Decimal("30")
TDS_RATE = Decimal("0.01")
TDS_THRESHOLD = Decimal("5000")                   # strictly greater than
FREEZE_SHORTFALL_THRESHOLD = Decimal("2000")      # strictly greater than (BR-09)

# Working-day rules
RULE_OUTCOME_EVENT = "OUTCOME_EVENT"
RULE_DELIVERED_ONLY = "DELIVERED_ONLY"
RULE_ROSTERED = "ROSTERED"
WORKING_DAY_RULES = (RULE_OUTCOME_EVENT, RULE_DELIVERED_ONLY, RULE_ROSTERED)

# Freeze hold modes
HOLD_ALL = "HOLD_ALL"
HOLD_SHORTFALL = "HOLD_SHORTFALL"
FREEZE_HOLD_MODES = (HOLD_ALL, HOLD_SHORTFALL)

# Line-item codes
CODE_DAILY_NET_TOTAL = "DAILY_NET_TOTAL"
CODE_ATTENDANCE_BONUS = "ATTENDANCE_BONUS"
CODE_EV_ALLOWANCE = "EV_ALLOWANCE"
CODE_TDS = "TDS"
CODE_TDS_BASE = "TDS_BASE"
CODE_ROUNDING = "NET_ROUNDING"
CODE_WORKING_DATES = "WORKING_DATES"
CODE_DAILY_REVIEW = "DAILY_RESULT_REVIEW"

# Warning codes
WARNING_MISSING_DAILY_RESULT = "MISSING_DAILY_RESULT"
WARNING_OUT_OF_PERIOD = "OUT_OF_PERIOD_RESULT"
WARNING_DUPLICATE_DAILY_RESULT = "DUPLICATE_DAILY_RESULT"
WARNING_OFF_ROSTER_WORK = "OFF_ROSTER_WORK"
WARNING_NO_DELIVERY_DAY = "NO_DELIVERY_DAY"
WARNING_NO_ACTIVITY = "NO_ACTIVITY"
WARNING_WEEKLY_FREEZE = "WEEKLY_FREEZE"
WARNING_WEEKLY_COD_TOTAL = "WEEKLY_COD_TOTAL_ABOVE_THRESHOLD"
WARNING_NEGATIVE_NET = "NEGATIVE_WEEKLY_NET"

STATUS_OK = "OK"
STATUS_REVIEW = "REVIEW"
TDS_APPLIED = "APPLIED"
TDS_NOT_APPLICABLE = "NOT_APPLICABLE"


# --------------------------------------------------------------------------
# Policy and result records
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WeeklyPolicy:
    """The choices the documents leave open (defaults are documented in the module doc)."""

    working_day_rule: str = RULE_OUTCOME_EVENT
    require_roster_shift: bool = True
    freeze_hold_mode: str = HOLD_ALL
    freeze_on_weekly_total: bool = False

    def __post_init__(self) -> None:
        if self.working_day_rule not in WORKING_DAY_RULES:
            raise ValueError(f"working_day_rule must be one of {WORKING_DAY_RULES}")
        if self.freeze_hold_mode not in FREEZE_HOLD_MODES:
            raise ValueError(f"freeze_hold_mode must be one of {FREEZE_HOLD_MODES}")


DEFAULT_WEEKLY_POLICY = WeeklyPolicy()


@dataclass(frozen=True)
class WeeklyDayLine:
    """What one Monday..Saturday date contributed to the week."""

    day: date
    rostered: bool
    has_daily_result: bool
    qualifies: bool                       # counted as a working day
    delivered_count: int
    outcome_event_count: int              # delivered + failed + rescheduled
    gross_earnings: Decimal               # copied from the daily result (0.00 if none)
    net_payout: Decimal                   # copied from the daily result (0.00 if none)
    cod_shortfall: Decimal
    freeze_flag: bool


@dataclass(frozen=True)
class WeeklyPayoutResult:
    rider_id: str
    vehicle_type: str
    week_start: date                      # Monday
    week_end: date                        # Saturday
    status: str                           # OK or REVIEW (see warnings)

    # attendance
    working_days: int
    working_dates: tuple[date, ...]
    daily_lines: tuple[WeeklyDayLine, ...]   # always the six Mon..Sat dates, in order

    # daily totals (copied/summed from the daily results, never recalculated)
    daily_gross_total: Decimal            # earnings before penalties / COD (the TDS base starts here)
    penalties_total: Decimal              # sum of the daily penalties actually deducted (after the 25% cap)
    daily_net_total: Decimal              # gross - penalties - COD shortfall, as the daily results state it

    # weekly-level items
    attendance_bonus: Decimal
    ev_allowance: Decimal
    weekly_gross: Decimal                 # daily_gross_total + bonuses, BEFORE any deduction
    tds_base: Decimal                     # == weekly_gross
    tds_amount: Decimal
    tds_status: str                       # APPLIED / NOT_APPLICABLE
    net_before_rounding: Decimal          # weekly_gross - tds - penalties_total - cod_shortfall_total
    net_rounded: Decimal                  # whole rupees (final rounding)

    # COD / freeze (reported from the daily results; NOT deducted again)
    cod_shortfall_total: Decimal
    max_daily_cod_shortfall: Decimal
    freeze_flag: bool
    freeze_dates: tuple[date, ...]
    amount_held: Decimal
    amount_payable: Decimal

    line_items: tuple[LineItem, ...]      # net-affecting lines add up to net_rounded
    warnings: tuple[LineItem, ...]
    assumptions: tuple[str, ...]

    @property
    def warning_codes(self) -> tuple[str, ...]:
        return tuple(w.code for w in self.warnings)

    @property
    def frozen(self) -> bool:
        return self.freeze_flag

    def to_csv_row(self) -> dict[str, str]:
        """Fields of the README ``weekly_payouts.csv`` (text, 2-dp money, true/false).

        ``weekly_gross`` is the gross BEFORE deductions (daily gross + bonuses = TDS base);
        ``daily_net_total`` is the sum of the daily nets (penalties and COD already deducted)."""
        def money(x: Decimal) -> str:
            return f"{x.quantize(PAISE, rounding=ROUND_HALF_UP):.2f}"

        return {
            "week_start": self.week_start.isoformat(),
            "rider_id": self.rider_id,
            "working_days": str(self.working_days),
            "daily_net_total": money(self.daily_net_total),
            "attendance_bonus": money(self.attendance_bonus),
            "ev_allowance": money(self.ev_allowance),
            "weekly_gross": money(self.weekly_gross),
            "tds": money(self.tds_amount),
            "net_rounded": money(self.net_rounded),
            "frozen": "true" if self.freeze_flag else "false",
        }

    def statement_lines(self) -> tuple[str, ...]:
        """A plain-text weekly statement (printing/command wiring belongs to the later runner)."""
        out = [
            f"WEEKLY STATEMENT  rider {self.rider_id} ({self.vehicle_type})",
            f"Week: {self.week_start.isoformat()} (Mon) to {self.week_end.isoformat()} (Sat)",
            f"Working days: {self.working_days} - " + (", ".join(d.isoformat() for d in self.working_dates) or "none"),
            f"Daily gross total:   {self.daily_gross_total:.2f}   (before penalties and COD shortfall)",
            f"Daily penalties:     {self.penalties_total:.2f}   (deducted once, in the daily net)",
            f"COD shortfall total: {self.cod_shortfall_total:.2f}   (deducted once, in the daily net; max single day {self.max_daily_cod_shortfall:.2f})",
            f"Daily net total:     {self.daily_net_total:.2f}   (= gross - penalties - COD shortfall)",
            f"Attendance bonus:    {self.attendance_bonus:.2f}",
            f"EV allowance:        {self.ev_allowance:.2f}",
            f"Weekly gross before deductions (TDS base): {self.weekly_gross:.2f}",
            f"TDS ({self.tds_status}): {self.tds_amount:.2f}",
            f"Net before rounding: {self.net_before_rounding:.2f}   (= weekly gross - TDS - penalties - COD shortfall)",
            f"Net rounded:         {self.net_rounded:.2f}",
            f"Freeze: {'FROZEN' if self.freeze_flag else 'not frozen'}"
            + (f" (days: {', '.join(d.isoformat() for d in self.freeze_dates)})" if self.freeze_dates else ""),
            f"Amount held:         {self.amount_held:.2f}",
            f"Amount payable:      {self.amount_payable:.2f}",
        ]
        out += [f"WARNING {w.code}: {w.description}" for w in self.warnings]
        out += [f"ASSUMPTION: {a}" for a in self.assumptions]
        return tuple(out)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _require_date(value: object, label: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{label} must be a datetime.date, got {type(value).__name__}")
    return value


def _require_monday(week_start: object) -> date:
    d = _require_date(week_start, "week_start")
    if d.weekday() != 0:
        raise ValueError(f"week_start {d.isoformat()} is a {d.strftime('%A')}; a payout week starts on a Monday")
    return d


def week_dates(week_start: date) -> tuple[date, ...]:
    """The six working dates Monday..Saturday of the week starting on ``week_start`` (a Monday)."""
    start = _require_monday(week_start)
    return tuple(start + timedelta(days=i) for i in range(WEEK_LENGTH_DAYS))


def _round_rupee(x: Decimal) -> Decimal:
    return x.quantize(RUPEE, rounding=ROUND_HALF_UP)


def compute_tds(weekly_gross: Decimal) -> Decimal:
    """1% of ``weekly_gross`` rounded HALF_UP to paise, only when it is STRICTLY above Rs 5,000."""
    if weekly_gross > TDS_THRESHOLD:
        return (weekly_gross * TDS_RATE).quantize(PAISE, rounding=ROUND_HALF_UP)
    return Decimal("0.00")


def _outcome_count(r: DailyPayoutResult) -> int:
    return r.delivered_count + r.failed_paid_count + r.failed_unpaid_count + r.rescheduled_count


def _day_qualifies(r: DailyPayoutResult, rostered: bool, policy: WeeklyPolicy) -> bool:
    if policy.require_roster_shift and not rostered:
        return False
    if policy.working_day_rule == RULE_ROSTERED:
        return rostered
    if policy.working_day_rule == RULE_DELIVERED_ONLY:
        return r.delivered_count > 0
    return _outcome_count(r) > 0


def _assumption_lines(policy: WeeklyPolicy) -> tuple[str, ...]:
    rule_text = {
        RULE_OUTCOME_EVENT: "a working day = rostered AND at least one DELIVERED/FAILED/RESCHEDULED event "
                            "(a FAILED-only day counts; EC-08 pending mentor confirmation)",
        RULE_DELIVERED_ONLY: "a working day = rostered AND at least one DELIVERED parcel "
                             "(a FAILED-only day does NOT count; EC-08 pending mentor confirmation)",
        RULE_ROSTERED: "a working day = a roster row, regardless of events",
    }[policy.working_day_rule]
    if not policy.require_roster_shift and policy.working_day_rule != RULE_ROSTERED:
        rule_text = rule_text.replace("rostered AND ", "")
    hold_text = ("a frozen week holds the entire net weekly payout" if policy.freeze_hold_mode == HOLD_ALL
                 else "a frozen week holds only an amount equal to the weekly COD shortfall (capped at the net)")
    return (
        rule_text,
        "TDS base = weekly gross BEFORE deductions (daily gross total + attendance bonus + EV allowance), so daily "
        "penalties and COD shortfalls do not reduce it; 1% rounded half-up to paise; applied only when strictly "
        "above 5000.00 (project working decision, not mentor-confirmed; the design document lists the TDS base as open)",
        "final net = weekly gross - TDS - daily penalties - COD shortfall, each deducted exactly once; "
        "rounded half-up to the nearest rupee as the last step",
        "freeze = any daily result with weekly_freeze_flag" + ("; or weekly shortfall total > 2000"
                                                              if policy.freeze_on_weekly_total else ""),
        hold_text,
        "COD shortfall and penalties are not deducted again (they are already inside the daily net)",
    )


# --------------------------------------------------------------------------
# The calculation
# --------------------------------------------------------------------------


def calculate_weekly_payout(ctx: PayoutContext, rider_id: str, week_start: date, *,
                            daily_results: Iterable[DailyPayoutResult] | None = None,
                            policy: WeeklyPolicy = DEFAULT_WEEKLY_POLICY) -> WeeklyPayoutResult:
    """Weekly payout of one rider for the Monday-Saturday week starting ``week_start``.

    daily_results: real ``DailyPayoutResult`` objects (any riders/dates; only this
        rider's results dated Monday..Saturday of the week are aggregated, the
        rest are ignored or reported). Omitted -> the existing daily API computes them.
    Raises ValueError for an unknown rider or a ``week_start`` that is not a Monday,
    TypeError for a non-date ``week_start``.
    """
    dates = week_dates(week_start)
    if rider_id not in ctx.riders:
        raise ValueError(f"unknown rider_id {rider_id!r}")
    if daily_results is None:
        supplied = tuple(calculate_daily_payout(ctx, rider_id, d)
                         for d in dates if rider_id in riders_for_date(ctx, d))
    else:
        supplied = tuple(daily_results)
    return _aggregate(ctx, rider_id, dates, supplied, policy)


def calculate_weekly_payouts(ctx: PayoutContext, week_start: date, *,
                             rider_ids: Iterable[str] | None = None,
                             daily_results: Iterable[DailyPayoutResult] | None = None,
                             policy: WeeklyPolicy = DEFAULT_WEEKLY_POLICY) -> tuple[WeeklyPayoutResult, ...]:
    """Weekly payouts for a batch of riders, sorted by rider_id.

    Default riders: everyone with a roster row or a daily result in the week
    (computed with the daily API when ``daily_results`` is omitted).
    """
    dates = week_dates(week_start)
    if daily_results is None:
        supplied = tuple(r for d in dates for r in calculate_daily_payouts(ctx, d))
    else:
        supplied = tuple(daily_results)
    if rider_ids is None:
        in_week = set(dates)
        ids = {s_rid for (s_rid, s_date) in ctx.shifts if s_date in in_week}
        ids |= {r.rider_id for r in supplied if r.operating_date in in_week}
        ids &= set(ctx.riders)
    else:
        ids = set(rider_ids)
    return tuple(_aggregate_checked(ctx, rid, dates, supplied, policy) for rid in sorted(ids))


def _aggregate_checked(ctx: PayoutContext, rider_id: str, dates: tuple[date, ...],
                       supplied: tuple[DailyPayoutResult, ...], policy: WeeklyPolicy) -> WeeklyPayoutResult:
    if rider_id not in ctx.riders:
        raise ValueError(f"unknown rider_id {rider_id!r}")
    return _aggregate(ctx, rider_id, dates, supplied, policy)


def _aggregate(ctx: PayoutContext, rider_id: str, dates: tuple[date, ...],
               supplied: tuple[DailyPayoutResult, ...], policy: WeeklyPolicy) -> WeeklyPayoutResult:
    rider = ctx.riders[rider_id]
    warns: list[LineItem] = []

    def warn(code: str, text: str, refs: tuple[str, ...] = ()) -> None:
        warns.append(LineItem(code, WARNING, text, Decimal(0), references=refs))

    # ---- pick this rider's results inside Monday..Saturday (deterministic, no mutation) ----
    by_date: dict[date, DailyPayoutResult] = {}
    for r in sorted((x for x in supplied if x.rider_id == rider_id), key=lambda x: x.operating_date):
        if r.operating_date not in dates:
            why = "a Sunday (not a working day)" if r.operating_date.weekday() == 6 else "outside the Monday-Saturday week"
            warn(WARNING_OUT_OF_PERIOD,
                 f"daily result of {r.operating_date.isoformat()} is {why}; not aggregated",
                 (r.operating_date.isoformat(),))
        elif r.operating_date in by_date:
            warn(WARNING_DUPLICATE_DAILY_RESULT,
                 f"more than one daily result for {r.operating_date.isoformat()}; the first is used, the rest ignored",
                 (r.operating_date.isoformat(),))
        else:
            by_date[r.operating_date] = r

    # ---- per-day lines and the working-day count ----
    lines: list[WeeklyDayLine] = []
    working: list[date] = []
    review_days: list[date] = []
    zero = Decimal("0.00")
    for d in dates:
        rostered = (rider_id, d) in ctx.shifts
        r = by_date.get(d)
        if r is None:
            if rostered:
                warn(WARNING_MISSING_DAILY_RESULT,
                     f"{d.isoformat()} is rostered but no daily payout result was supplied; it contributes nothing "
                     "and is not counted as a working day", (d.isoformat(),))
            lines.append(WeeklyDayLine(d, rostered, False, False, 0, 0, zero, zero, zero, False))
            continue
        qualifies = _day_qualifies(r, rostered, policy)
        if qualifies:
            working.append(d)
        outcomes = _outcome_count(r)
        if outcomes > 0 and not rostered:
            warn(WARNING_OFF_ROSTER_WORK,
                 f"{d.isoformat()}: events recorded without a roster shift; the day is paid through the daily net but "
                 + ("is not counted as a working day" if not qualifies else "is counted (require_roster_shift is off)"),
                 (d.isoformat(),))
        if outcomes > 0 and r.delivered_count == 0:
            warn(WARNING_NO_DELIVERY_DAY,
                 f"{d.isoformat()}: attempts recorded but nothing delivered (e.g. FAILED only); "
                 + ("counted" if qualifies else "NOT counted")
                 + f" as a working day under rule {policy.working_day_rule} - mentor confirmation pending (EC-08)",
                 (d.isoformat(),))
        if r.status != "OK":
            review_days.append(d)
        lines.append(WeeklyDayLine(d, rostered, True, qualifies, r.delivered_count, outcomes, r.gross_earnings,
                                   r.net_payout, r.cod_shortfall, r.weekly_freeze_flag))

    if not by_date and not any(line.rostered for line in lines):
        warn(WARNING_NO_ACTIVITY, "no roster rows and no daily results in this week")

    # ---- totals (summed from the daily results; nothing is recalculated) ----
    daily_gross = sum((x.gross_earnings for x in by_date.values()), Decimal(0))
    daily_net = sum((x.net_payout for x in by_date.values()), Decimal(0))
    penalties_total = sum((x.penalties_deducted for x in by_date.values()), Decimal(0))
    shortfall_total = sum((x.cod_shortfall for x in by_date.values()), Decimal(0))
    max_shortfall = max((x.cod_shortfall for x in by_date.values()), default=Decimal(0))
    freeze_dates = tuple(d for d in dates if d in by_date and by_date[d].weekly_freeze_flag)

    # ---- weekly-level items ----
    n_work = len(working)
    attendance = ATTENDANCE_BONUS if n_work == ATTENDANCE_REQUIRED_DAYS else Decimal(0)
    ev = EV_ALLOWANCE_PER_DAY * n_work if rider.vehicle_type == EV_BIKE_VEHICLE_TYPE else Decimal(0)
    # TDS base: gross earnings + weekly bonuses, BEFORE daily penalties / COD shortfall.
    weekly_gross = daily_gross + attendance + ev
    tds = compute_tds(weekly_gross)
    tds_status = TDS_APPLIED if tds > 0 else TDS_NOT_APPLICABLE
    # Final net: the daily NET already holds penalties and COD shortfall (deducted once there), so we add
    # the weekly items to it and take TDS off. This equals weekly_gross - tds - penalties - COD shortfall.
    net_before = daily_net + attendance + ev - tds
    net_rounded = _round_rupee(net_before)
    if net_rounded < 0:
        warn(WARNING_NEGATIVE_NET, f"weekly net payout is negative ({net_rounded}); it is reported as is")

    # ---- freeze ----
    freeze = bool(freeze_dates)
    if shortfall_total > FREEZE_SHORTFALL_THRESHOLD and not freeze:
        if policy.freeze_on_weekly_total:
            freeze = True
        else:
            warn(WARNING_WEEKLY_COD_TOTAL,
                 f"weekly COD shortfall {shortfall_total} is above {FREEZE_SHORTFALL_THRESHOLD} but no single day was; "
                 "not frozen under the default policy")
    if freeze:
        warn(WARNING_WEEKLY_FREEZE,
             f"weekly payout frozen pending review (COD shortfall above {FREEZE_SHORTFALL_THRESHOLD} on "
             + (", ".join(d.isoformat() for d in freeze_dates) if freeze_dates else "weekly total") + ")",
             tuple(d.isoformat() for d in freeze_dates))
    positive_net = max(net_rounded, Decimal(0))
    if not freeze:
        held = Decimal(0)
    elif policy.freeze_hold_mode == HOLD_ALL:
        held = positive_net
    else:
        held = min(positive_net, shortfall_total)
    payable = net_rounded - held

    # ---- auditable lines: EARNING/DEDUCTION/ADJUSTMENT lines add up to net_rounded ----
    items: list[LineItem] = [LineItem(CODE_DAILY_NET_TOTAL, EARNING,
                                      f"sum of {len(by_date)} daily net payout(s); daily penalties and COD shortfall already included",
                                      daily_net, Decimal(len(by_date)))]
    if attendance:
        items.append(LineItem(CODE_ATTENDANCE_BONUS, EARNING, f"attendance bonus for {n_work} working days", attendance))
    if ev:
        items.append(LineItem(CODE_EV_ALLOWANCE, EARNING, f"EV_BIKE allowance for {n_work} working days at {EV_ALLOWANCE_PER_DAY}/day",
                              ev, Decimal(n_work), EV_ALLOWANCE_PER_DAY))
    if tds:
        items.append(LineItem(CODE_TDS, DEDUCTION,
                              f"TDS 1% of the TDS base {weekly_gross} (daily gross + weekly bonuses, before penalties/COD)", -tds))
    if net_rounded != net_before:
        items.append(LineItem(CODE_ROUNDING, ADJUSTMENT, "rounding to the nearest rupee", net_rounded - net_before))
    items.append(LineItem(CODE_TDS_BASE, INFO,
                          f"TDS base {weekly_gross} = daily gross {daily_gross} + attendance {attendance} + EV {ev}; "
                          f"not reduced by penalties {penalties_total} or COD shortfall {shortfall_total}", Decimal(0)))
    items.append(LineItem(CODE_WORKING_DATES, INFO, ", ".join(d.isoformat() for d in working) or "none", Decimal(0)))
    if review_days:   # information only: the daily results keep their own warnings; they are not repeated here
        items.append(LineItem(CODE_DAILY_REVIEW, INFO,
                              "daily results with status REVIEW (see their own warnings): "
                              + ", ".join(d.isoformat() for d in review_days), Decimal(0)))

    return WeeklyPayoutResult(
        rider_id=rider_id, vehicle_type=rider.vehicle_type, week_start=dates[0], week_end=dates[-1],
        status=STATUS_REVIEW if warns else STATUS_OK,
        working_days=n_work, working_dates=tuple(working), daily_lines=tuple(lines),
        daily_gross_total=daily_gross, penalties_total=penalties_total, daily_net_total=daily_net,
        attendance_bonus=attendance, ev_allowance=ev, weekly_gross=weekly_gross, tds_base=weekly_gross,
        tds_amount=tds, tds_status=tds_status, net_before_rounding=net_before, net_rounded=net_rounded,
        cod_shortfall_total=shortfall_total, max_daily_cod_shortfall=max_shortfall, freeze_flag=freeze,
        freeze_dates=freeze_dates, amount_held=held, amount_payable=payable,
        line_items=tuple(items) + tuple(warns), warnings=tuple(warns), assumptions=_assumption_lines(policy),
    )
