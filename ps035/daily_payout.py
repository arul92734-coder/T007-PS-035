"""Daily payout calculation for ONE rider and ONE operating date (BR-05 .. BR-09).

Beginner notes
--------------
* This module only CALCULATES a daily payout. It reads the project's real
  records (loader records, the ``EventLog`` from ``ps035.delivery_events``,
  and routes from ``ps035.route_sequencing``) and never writes any file. It
  does NOT do weekly attendance bonus, EV allowance, TDS or weekly freeze
  settlement. For BR-09 it only REPORTS ``weekly_freeze_flag`` and the
  shortfall details for a later weekly module.
* Money is ``Decimal`` everywhere. Amounts are rounded to paise (0.01) only
  where a calculation creates more than 2 decimals (distance pay, the 25% cap).
  There is NO rupee rounding in the daily result (BR-10 rounds the WEEKLY net).
* Event history is authoritative: events outside a rider's shift are paid.

Public API
----------
``PayoutContext.from_dataset(ds)``       bundle the loaded records + EventLog
``calculate_daily_payout(ctx, rider_id, operating_date, ...) -> DailyPayoutResult``
``calculate_daily_payouts(ctx, operating_date, ...) -> tuple[DailyPayoutResult, ...]``
``derive_planned_route(ctx, rider_id, operating_date) -> RouteResult | None``
``planned_overtime_km(route) -> Decimal``

Formula
-------
    gross = delivery_pay + regular_distance_pay + overtime_distance_pay
            + volume_incentive + first_attempt_incentive
    penalties_uncapped = 50*RIDER_FAULT + 20*late + 100*complaints
    penalty_cap        = 25% of gross (rounded DOWN to paise)
    penalties_deducted = min(penalties_uncapped, penalty_cap)      # cap applied ONCE
    net = gross - penalties_deducted - cod_shortfall                # may be negative; never floored

Every amount is also listed as a ``LineItem``; the signed amounts of the
net-affecting items add up exactly to ``net`` (tested).

BR-05  per-parcel pay (which event is payable)
    Outcome events (DELIVERED / FAILED / RESCHEDULED) of the rider on the
    operating date are grouped by (parcel_id, attempt_no). ONE event per group
    is the payable outcome: DELIVERED, else FAILED, else RESCHEDULED (ties:
    earliest by the EventLog's (event_ts, event_id) order). Any other event in
    the group is ignored and reported (a FAILED followed by RESCHEDULED of the
    same attempt is normal and not reported). So a parcel is never paid twice for
    one attempt, and a DELIVERED parcel can only be paid once.
    DELIVERED pays 18 (STANDARD) / 25 (EXPRESS) by the parcel's REGISTERED
    priority (``ParcelLifecycle.original_priority`` = parcels.csv). The BR-11
    upgrade of a rescheduled STANDARD parcel to EXPRESS steers pooling and
    routing; the official sample README prices RDR-07's two re-delivered
    parcels at "~Rs 36" (2 x 18), so the upgrade does not raise the rate. Policy
    ``pay_upgraded_priority_as_express=True`` pays the upgraded (current)
    priority instead. FAILED NOT_HOME / WRONG_ADDRESS pays 10;
    RIDER_FAULT pays 0; any other reason (e.g. CUSTOMER_BUSY) pays 0 and is
    reported as UNRECOGNISED_FAILURE_REASON. RESCHEDULED pays 0.

BR-06  distance pay
    planned_km  = ``RouteResult.total_km`` (all legs incl. the return leg).
    actual_km   = odometer end_km - start_km.
    actual_km <= planned_km * 1.10 (exactly +10% included)  -> use actual_km.
    actual_km >  planned_km * 1.10                           -> use planned_km
                                                               and set odometer_flag.
    Only the upper side is tested (README: "odometer_flag true when actual is
    more than 10% above planned"); an actual below planned is used as is.
    Overtime: see ``planned_overtime_km``. Regular km are paid 3.50/km, overtime
    km 3.50 * 1.5 = 5.25/km; each km is paid ONCE (regular_km = selected_km -
    overtime_km). If the odometer is used, the planned overtime SHARE
    (overtime_km / planned_km) is applied to it.
    No planned route -> distance pay is 0.00 and NO_PLANNED_ROUTE is reported
    (a distance is never invented). No odometer row -> planned_km is used and
    MISSING_ODOMETER is reported.

BR-07  incentives
    Volume (by DELIVERED count): >=45 -> 250, >=30 -> 100 (not cumulative).
    First-attempt: numerator = DELIVERED payable events with attempt_no == 1;
    denominator = all payable outcome events of the day (DELIVERED, FAILED and,
    by default, RESCHEDULED). Rate = numerator / denominator, tested with exact
    integer arithmetic (``num * 100 >= 95 * den``) so exactly 95% qualifies. No
    outcome events -> no rate, no bonus. Parcels with no event are not counted.

BR-08  penalties
    50 per payable FAILED/RIDER_FAULT attempt, 20 per payable DELIVERED event
    later than the parcel's window_end (exactly window_end is on time), 100 per
    COMPLAINT event (by event_id). Total capped once at 25% of gross.

BR-09  COD
    collected = sum of ``cod_collected`` of payable DELIVERED events (nothing
    from other event types). deposits = the rider's cod_deposits rows with
    deposit_date == operating date. Deadline 21:00:00 same day: <= is on time,
    21:00:01 is late.
    shortfall = max(0, collected - credited_deposits); a surplus is reported
    (cod_surplus) and never becomes a negative deduction or a credit.
    LATE DEPOSITS: the documents say "deposit by 21:00" and "shortfall =
    collected - deposited" but give no late penalty or exclusion. Default policy
    ``COUNT_AND_FLAG``: a late deposit still reduces the shortfall (the cash was
    received) but is NOT an on-time deposit: deposit_on_time is False and
    LATE_DEPOSIT is reported. Alternative ``EXCLUDE``: late deposits are not
    credited, so the late amount stays in the shortfall. shortfall > 2000
    (strictly) sets ``weekly_freeze_flag``; exactly 2000 does not.

Missing data never silently becomes a clean result: the result ``status`` is
"REVIEW" and ``warnings`` lists reason codes whenever anything was missing,
partial, flagged or unusual (see the WARNING_* constants).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Iterable, Mapping

from ps035.delivery_events import (
    COMPLAINT, DELIVERED, EXPRESS, FAILED, RESCHEDULED, EventLog,
)
from ps035.loader import (
    CodDeposit, DataSet, DeliveryEvent, Hub, OdometerReading, Parcel, Rider, RiderShift,
)
from ps035.route_sequencing import RouteResult, sequence_route

# --------------------------------------------------------------------------
# Business-rule constants (all money is Decimal)
# --------------------------------------------------------------------------

PAISE = Decimal("0.01")
KM_QUANTUM = Decimal("0.01")

RATE_STANDARD = Decimal("18")                    # BR-05
RATE_EXPRESS = Decimal("25")
FAILED_ATTEMPT_PAY = Decimal("10")
FAILED_PAID_REASONS = ("NOT_HOME", "WRONG_ADDRESS")
RIDER_FAULT = "RIDER_FAULT"

DISTANCE_RATE_PER_KM = Decimal("3.50")           # BR-06
OVERTIME_MULTIPLIER = Decimal("1.5")
ODOMETER_TOLERANCE = Decimal("1.10")             # actual may be up to +10%

VOLUME_TIER_1_PARCELS, VOLUME_TIER_1_AMOUNT = 30, Decimal("100")   # BR-07
VOLUME_TIER_2_PARCELS, VOLUME_TIER_2_AMOUNT = 45, Decimal("250")
FIRST_ATTEMPT_MIN_PERCENT = 95
FIRST_ATTEMPT_BONUS = Decimal("50")

RIDER_FAULT_PENALTY = Decimal("50")              # BR-08
LATE_DELIVERY_PENALTY = Decimal("20")
COMPLAINT_PENALTY = Decimal("100")
PENALTY_CAP_RATE = Decimal("0.25")

COD_DEADLINE = time(21, 0, 0)                    # BR-09
FREEZE_SHORTFALL_THRESHOLD = Decimal("2000")     # strictly greater than

# Late-deposit policies (see module doc)
LATE_COUNT_AND_FLAG = "COUNT_AND_FLAG"
LATE_EXCLUDE = "EXCLUDE"

# Line-item kinds
EARNING, DEDUCTION, ADJUSTMENT, INFO, WARNING = "EARNING", "DEDUCTION", "ADJUSTMENT", "INFO", "WARNING"
NET_KINDS = (EARNING, DEDUCTION, ADJUSTMENT)

# Reason / line-item codes
CODE_DELIVERY_PAY = "DELIVERY_PAY"
CODE_FAILED_PAY = "FAILED_ATTEMPT_PAY"
CODE_FAILED_NO_PAY = "FAILED_ATTEMPT_NO_PAY"
CODE_RESCHEDULED = "RESCHEDULED_NO_PAY"
CODE_DISTANCE_REGULAR = "DISTANCE_PAY_REGULAR"
CODE_DISTANCE_OVERTIME = "DISTANCE_PAY_OVERTIME"
CODE_VOLUME_INCENTIVE = "VOLUME_INCENTIVE"
CODE_FIRST_ATTEMPT_INCENTIVE = "FIRST_ATTEMPT_INCENTIVE"
CODE_PENALTY_RIDER_FAULT = "PENALTY_RIDER_FAULT"
CODE_PENALTY_LATE = "PENALTY_LATE_DELIVERY"
CODE_PENALTY_COMPLAINT = "PENALTY_COMPLAINT"
CODE_PENALTY_CAP_RELIEF = "PENALTY_CAP_RELIEF"
CODE_COD_SHORTFALL = "COD_SHORTFALL_DEDUCTION"

WARNING_NO_EVENTS = "NO_EVENTS"
WARNING_NO_PLANNED_ROUTE = "NO_PLANNED_ROUTE"
WARNING_PLANNED_ROUTE_PARTIAL = "PLANNED_ROUTE_PARTIAL"
WARNING_MISSING_ODOMETER = "MISSING_ODOMETER"
WARNING_ODOMETER_DISCREPANCY = "ODOMETER_DISCREPANCY"
WARNING_UNRECOGNISED_REASON = "UNRECOGNISED_FAILURE_REASON"
WARNING_DUPLICATE_EVENT = "DUPLICATE_ATTEMPT_EVENT_IGNORED"
WARNING_LATE_DEPOSIT = "LATE_DEPOSIT"
WARNING_NO_DEPOSIT = "NO_COD_DEPOSIT"
WARNING_COD_SURPLUS = "COD_SURPLUS"
WARNING_WEEKLY_FREEZE = "WEEKLY_FREEZE_FLAG"
WARNING_NEGATIVE_NET = "NEGATIVE_NET"

STATUS_OK = "OK"
STATUS_REVIEW = "REVIEW"

DISTANCE_SOURCE_ODOMETER = "ODOMETER"
DISTANCE_SOURCE_PLANNED = "PLANNED"
DISTANCE_SOURCE_NONE = "NONE"


# --------------------------------------------------------------------------
# Policy, context and result records
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PayoutPolicy:
    """The three choices the documents leave open (defaults are documented above)."""

    late_deposit_treatment: str = LATE_COUNT_AND_FLAG
    reschedule_counts_as_first_attempt_miss: bool = True
    pay_upgraded_priority_as_express: bool = False

    def __post_init__(self) -> None:
        if self.late_deposit_treatment not in (LATE_COUNT_AND_FLAG, LATE_EXCLUDE):
            raise ValueError(f"late_deposit_treatment must be {LATE_COUNT_AND_FLAG!r} or {LATE_EXCLUDE!r}")


DEFAULT_POLICY = PayoutPolicy()


@dataclass(frozen=True)
class PayoutContext:
    """The real project records one payout run needs (nothing is copied or changed)."""

    event_log: EventLog
    parcels: Mapping[str, Parcel]
    riders: Mapping[str, Rider]
    hubs: Mapping[str, Hub]
    shifts: Mapping[tuple[str, date], RiderShift]
    odometer: Mapping[tuple[str, date], OdometerReading]
    deposits: Mapping[tuple[str, date], tuple[CodDeposit, ...]]

    @classmethod
    def from_dataset(cls, ds: DataSet, event_log: EventLog | None = None) -> "PayoutContext":
        """Build from ``load_dataset`` output. ``event_log`` defaults to ``EventLog.from_dataset(ds)``."""
        deposits: dict[tuple[str, date], list[CodDeposit]] = {}
        for d in sorted(ds.cod_deposits, key=lambda x: (x.deposit_ts, x.deposit_id)):
            deposits.setdefault((d.rider_id, d.deposit_date), []).append(d)
        return cls(
            event_log=event_log if event_log is not None else EventLog.from_dataset(ds),
            parcels={p.parcel_id: p for p in ds.parcels},
            riders={r.rider_id: r for r in ds.riders},
            hubs={h.hub_id: h for h in ds.hubs},
            shifts={(s.rider_id, s.shift_date): s for s in ds.shifts},
            odometer={(o.rider_id, o.odo_date): o for o in ds.odometer},
            deposits={k: tuple(v) for k, v in deposits.items()},
        )


@dataclass(frozen=True)
class LineItem:
    """One auditable line. ``amount`` is signed: deductions are negative.

    EARNING / DEDUCTION / ADJUSTMENT lines add up to the net payout; INFO and
    WARNING lines have amount 0 and only explain.
    """

    code: str
    kind: str
    description: str
    amount: Decimal
    quantity: Decimal | None = None
    unit_amount: Decimal | None = None
    references: tuple[str, ...] = ()   # parcel / event / deposit ids


@dataclass(frozen=True)
class DailyPayoutResult:
    rider_id: str
    operating_date: date
    status: str                              # OK or REVIEW (see warnings)

    # parcels / attempts
    delivered_count: int
    standard_delivered_count: int
    express_delivered_count: int
    failed_paid_count: int                   # FAILED attempts that earned the 10
    failed_unpaid_count: int
    rescheduled_count: int
    first_attempt_successes: int
    eligible_attempt_count: int
    first_attempt_success_rate_pct: Decimal | None   # 2 dp, display only

    # pay components
    delivery_pay: Decimal
    delivery_pay_standard: Decimal
    delivery_pay_express: Decimal
    failed_attempt_pay: Decimal

    # distance
    planned_distance_km: Decimal | None
    odometer_distance_km: Decimal | None
    selected_distance_km: Decimal | None
    distance_source: str
    odometer_discrepancy_flag: bool
    regular_distance_km: Decimal
    overtime_distance_km: Decimal
    regular_rate_per_km: Decimal
    overtime_rate_per_km: Decimal
    regular_distance_pay: Decimal
    overtime_distance_pay: Decimal

    # incentives
    volume_incentive: Decimal
    first_attempt_incentive: Decimal

    # penalties
    rider_fault_count: int
    late_delivery_count: int
    complaint_count: int
    rider_fault_penalty: Decimal
    late_delivery_penalty: Decimal
    complaint_penalty: Decimal
    penalties_uncapped: Decimal
    penalty_cap: Decimal
    penalties_deducted: Decimal

    # COD
    cod_collected: Decimal
    cod_deposited_total: Decimal             # every deposit row of the day
    cod_deposited_on_time: Decimal
    cod_deposited_late: Decimal
    cod_credited: Decimal                    # what was counted against the collected cash
    cod_surplus: Decimal
    cod_shortfall: Decimal
    deposit_count: int
    last_deposit_ts: datetime | None
    deposit_on_time: bool | None             # None = no deposit row
    weekly_freeze_flag: bool

    # totals
    gross_earnings: Decimal
    total_deductions: Decimal                # penalties_deducted + cod_shortfall
    net_payout: Decimal

    line_items: tuple[LineItem, ...]
    warnings: tuple[LineItem, ...]

    @property
    def warning_codes(self) -> tuple[str, ...]:
        return tuple(w.code for w in self.warnings)

    @property
    def distance_pay(self) -> Decimal:
        return self.regular_distance_pay + self.overtime_distance_pay

    def to_csv_row(self) -> dict[str, str]:
        """Fields of the README ``daily_payouts.csv`` (text, README formats).

        ``distance_pay`` = regular-km pay, ``overtime_pay`` = overtime-km pay
        (both are in ``gross``). Empty text where a distance is unknown.
        Writing the file belongs to the (later) command runner.
        """
        def money(x: Decimal) -> str:
            return f"{x.quantize(PAISE, rounding=ROUND_HALF_UP):.2f}"

        def km(x: Decimal | None) -> str:
            return "" if x is None else f"{x.quantize(KM_QUANTUM, rounding=ROUND_HALF_UP):.2f}"

        return {
            "date": self.operating_date.isoformat(),
            "rider_id": self.rider_id,
            "delivered": str(self.delivered_count),
            "failed_paid": str(self.failed_paid_count),
            "parcel_pay": money(self.delivery_pay + self.failed_attempt_pay),
            "planned_km": km(self.planned_distance_km),
            "actual_km": km(self.odometer_distance_km),
            "km_used": km(self.selected_distance_km),
            "odometer_flag": "true" if self.odometer_discrepancy_flag else "false",
            "distance_pay": money(self.regular_distance_pay),
            "overtime_pay": money(self.overtime_distance_pay),
            "incentive": money(self.volume_incentive),
            "first_attempt_bonus": money(self.first_attempt_incentive),
            "gross": money(self.gross_earnings),
            "penalties_raw": money(self.penalties_uncapped),
            "penalties_capped": money(self.penalties_deducted),
            "cod_shortfall": money(self.cod_shortfall),
            "net": money(self.net_payout),
        }


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _q(x: Decimal, rounding=ROUND_HALF_UP) -> Decimal:
    return x.quantize(PAISE, rounding=rounding)


def _dec(x: float | Decimal | int) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(repr(x))


def _micros(td: timedelta) -> int:
    return td // timedelta(microseconds=1)


def _require_date(value: object, label: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{label} must be a datetime.date, got {type(value).__name__}")
    return value


# --------------------------------------------------------------------------
# Planned route / overtime
# --------------------------------------------------------------------------


def planned_overtime_km(route: RouteResult) -> Decimal:
    """Planned kilometres DRIVEN after ``route.shift_end`` (BR-04 overtime portion), 2 dp.

    The documents say overtime is "work beyond shift end" paid at 1.5x "for the
    overtime portion" but do not say how a route's distance is split. Testable
    assumption: walk the planned timeline (each leg is driven between the
    previous departure and this stop's arrival; the return leg ends at
    ``return_time``). A leg finished by shift_end is regular; a leg that starts at
    or after shift_end is entirely overtime; a leg that crosses shift_end is split
    in proportion to driving time (speed is constant, 4 min/km). Waiting and
    service time are not distance. A route that is back by shift_end has 0.
    """
    end = route.shift_end
    legs: list[tuple[timedelta, timedelta, Decimal]] = []
    depart = route.shift_start
    for stop in route.stops:
        legs.append((depart, stop.arrival, _dec(stop.leg_km)))
        depart = stop.departure
    legs.append((depart, route.return_time, _dec(route.return_leg_km)))

    total = Decimal(0)
    for leg_depart, leg_arrive, km in legs:
        if km == 0 or leg_arrive <= end:
            continue
        if leg_depart >= end:
            total += km
        else:  # leg crosses shift_end: pro-rate by driving time
            total += km * Decimal(_micros(leg_arrive - end)) / Decimal(_micros(leg_arrive - leg_depart))
    return total.quantize(KM_QUANTUM, rounding=ROUND_HALF_UP)


def _outcome_events(events: Iterable[DeliveryEvent]) -> tuple[
        list[DeliveryEvent], list[DeliveryEvent], list[tuple[DeliveryEvent, DeliveryEvent]]]:
    """Split a rider-day's events into (payable outcomes, complaints, ignored duplicates).

    ``ignored`` holds (ignored_event, chosen_event) pairs that deserve a warning.
    """
    precedence = {DELIVERED: 0, FAILED: 1, RESCHEDULED: 2}
    ordered = list(events)
    position = {e.event_id: i for i, e in enumerate(ordered)}
    groups: dict[tuple[str, int], list[DeliveryEvent]] = {}
    complaints: list[DeliveryEvent] = []
    seen_complaints: set[str] = set()
    for e in ordered:
        if e.event_type == COMPLAINT:
            if e.event_id not in seen_complaints:
                seen_complaints.add(e.event_id)
                complaints.append(e)
            continue
        groups.setdefault((e.parcel_id, e.attempt_no), []).append(e)

    chosen: list[DeliveryEvent] = []
    ignored: list[tuple[DeliveryEvent, DeliveryEvent]] = []
    for group in groups.values():
        group.sort(key=lambda e: (precedence.get(e.event_type, 9), position[e.event_id]))
        best = group[0]
        chosen.append(best)
        for other in group[1:]:
            if not (best.event_type == FAILED and other.event_type == RESCHEDULED):
                ignored.append((other, best))
    chosen.sort(key=lambda e: position[e.event_id])
    return chosen, complaints, ignored


def derive_planned_route(ctx: PayoutContext, rider_id: str, operating_date: date) -> RouteResult | None:
    """Plan the rider's day from the project's own sequencer and real events.

    The sample data does not store planned distance. The parcels the rider
    actually attempted that day (payable outcome events) are sequenced with the
    existing ``sequence_route`` from the rider's home hub and that day's roster
    shift, with each parcel's priority at the start of the day (BR-11). For the
    sample this reproduces the README's EC-03 / EC-04 route (13.50 km).
    Returns None when the rider has no roster shift that day (no route can be
    built). If you have the PLAN result, pass it as ``planned_routes`` instead.
    Parcels the sequencer cannot place appear in ``RouteResult.unsequenced``.
    """
    _require_date(operating_date, "operating_date")
    rider = ctx.riders.get(rider_id)
    shift = ctx.shifts.get((rider_id, operating_date))
    if rider is None or shift is None:
        return None
    hub = ctx.hubs[rider.home_hub]
    events = ctx.event_log.query(rider_id=rider_id, event_date=operating_date)
    outcomes, _, _ = _outcome_events(events)
    parcels: list[Parcel] = []
    for pid in sorted({e.parcel_id for e in outcomes}):
        lc = ctx.event_log.lifecycle(pid, before=operating_date)
        parcels.append(replace(ctx.parcels[pid], priority=lc.current_priority))
    return sequence_route(hub.lat, hub.lon, shift.shift_start, shift.shift_end, parcels, hub_id=hub.hub_id)


_DERIVE = object()   # "derive the route from the events" (the default)


# --------------------------------------------------------------------------
# The calculation
# --------------------------------------------------------------------------


def calculate_daily_payout(ctx: PayoutContext, rider_id: str, operating_date: date, *,
                           planned_route: RouteResult | None | object = _DERIVE,
                           policy: PayoutPolicy = DEFAULT_POLICY) -> DailyPayoutResult:
    """Daily payout of one rider for one operating date (see module doc).

    planned_route: omitted -> ``derive_planned_route``; a ``RouteResult`` (e.g.
        ``RiderAssignment.route`` from PLAN) -> used as the planned route;
        ``None`` -> "no planned route available".
    Raises ValueError for an unknown rider and TypeError for a bad date.
    """
    _require_date(operating_date, "operating_date")
    if rider_id not in ctx.riders:
        raise ValueError(f"unknown rider_id {rider_id!r}")
    if planned_route is _DERIVE:
        planned_route = derive_planned_route(ctx, rider_id, operating_date)
    elif planned_route is not None and not isinstance(planned_route, RouteResult):
        raise TypeError("planned_route must be a RouteResult, None, or omitted")

    items: list[LineItem] = []
    warns: list[LineItem] = []

    def warn(code: str, text: str, refs: tuple[str, ...] = ()) -> None:
        warns.append(LineItem(code, WARNING, text, Decimal(0), references=refs))

    events = ctx.event_log.query(rider_id=rider_id, event_date=operating_date)
    outcomes, complaints, ignored = _outcome_events(events)
    for other, best in ignored:
        warn(WARNING_DUPLICATE_EVENT,
             f"{other.event_type} {other.event_id} ignored: attempt {best.attempt_no} of {best.parcel_id} "
             f"is already paid/decided by {best.event_type} {best.event_id}", (other.event_id, best.event_id))
    if not events:
        warn(WARNING_NO_EVENTS, "no delivery events recorded for this rider on this date")

    # ---- BR-05 per-parcel pay, BR-07 counts, BR-08 late / rider-fault, BR-09 COD collected ----
    n_std = n_exp = n_fail_paid = n_fail_unpaid = n_resched = first_ok = 0
    pay_std = pay_exp = pay_failed = Decimal(0)
    cod_collected = Decimal(0)
    late_refs: list[str] = []
    fault_refs: list[str] = []
    for e in outcomes:
        if e.event_type == DELIVERED:
            lc = ctx.event_log.lifecycle(e.parcel_id, before=operating_date)
            priority = lc.current_priority if policy.pay_upgraded_priority_as_express else lc.original_priority
            if priority == EXPRESS:
                n_exp += 1
                pay_exp += RATE_EXPRESS
                rate = RATE_EXPRESS
            else:
                n_std += 1
                pay_std += RATE_STANDARD
                rate = RATE_STANDARD
            if e.attempt_no == 1:
                first_ok += 1
            cod_collected += e.cod_collected
            items.append(LineItem(CODE_DELIVERY_PAY, EARNING,
                                  f"{priority} parcel {e.parcel_id} delivered (attempt {e.attempt_no})",
                                  rate, Decimal(1), rate, (e.event_id, e.parcel_id)))
            parcel = ctx.parcels[e.parcel_id]
            if e.event_ts > datetime.combine(e.event_ts.date(), parcel.window_end):
                late_refs.append(e.event_id)
        elif e.event_type == FAILED:
            if e.reason_code in FAILED_PAID_REASONS:
                n_fail_paid += 1
                pay_failed += FAILED_ATTEMPT_PAY
                items.append(LineItem(CODE_FAILED_PAY, EARNING,
                                      f"failed attempt {e.attempt_no} of {e.parcel_id} ({e.reason_code})",
                                      FAILED_ATTEMPT_PAY, Decimal(1), FAILED_ATTEMPT_PAY, (e.event_id, e.parcel_id)))
            else:
                n_fail_unpaid += 1
                items.append(LineItem(CODE_FAILED_NO_PAY, INFO,
                                      f"failed attempt {e.attempt_no} of {e.parcel_id} ({e.reason_code}) earns no pay",
                                      Decimal(0), references=(e.event_id, e.parcel_id)))
                if e.reason_code == RIDER_FAULT:
                    fault_refs.append(e.event_id)
                else:
                    warn(WARNING_UNRECOGNISED_REASON,
                         f"{e.event_id}: failure reason {e.reason_code!r} is not NOT_HOME, WRONG_ADDRESS or "
                         f"RIDER_FAULT; no failed-attempt pay", (e.event_id, e.parcel_id))
        else:  # RESCHEDULED
            n_resched += 1
            items.append(LineItem(CODE_RESCHEDULED, INFO, f"{e.parcel_id} rescheduled: no delivery pay",
                                  Decimal(0), references=(e.event_id, e.parcel_id)))

    delivered = n_std + n_exp
    delivery_pay = pay_std + pay_exp

    # ---- BR-07 incentives ----
    denominator = delivered + n_fail_paid + n_fail_unpaid
    if policy.reschedule_counts_as_first_attempt_miss:
        denominator += n_resched
    rate_pct: Decimal | None = None
    fa_incentive = Decimal(0)
    if denominator:
        rate_pct = (Decimal(first_ok * 100) / Decimal(denominator)).quantize(PAISE, rounding=ROUND_HALF_UP)
        if first_ok * 100 >= FIRST_ATTEMPT_MIN_PERCENT * denominator:
            fa_incentive = FIRST_ATTEMPT_BONUS
    if delivered >= VOLUME_TIER_2_PARCELS:
        volume = VOLUME_TIER_2_AMOUNT
    elif delivered >= VOLUME_TIER_1_PARCELS:
        volume = VOLUME_TIER_1_AMOUNT
    else:
        volume = Decimal(0)

    # ---- BR-06 distance ----
    planned_km: Decimal | None = None
    ot_planned = Decimal(0)
    if planned_route is not None:
        if planned_route.unsequenced and not planned_route.stops:
            warn(WARNING_NO_PLANNED_ROUTE,
                 f"planned route has no stops; {len(planned_route.unsequenced)} parcel(s) could not be sequenced, "
                 "so no planned distance can be derived")
        else:
            planned_km = _dec(planned_route.total_km).quantize(KM_QUANTUM, rounding=ROUND_HALF_UP)
            ot_planned = planned_overtime_km(planned_route)
            if planned_route.unsequenced:
                warn(WARNING_PLANNED_ROUTE_PARTIAL,
                     f"{len(planned_route.unsequenced)} parcel(s) could not be sequenced; planned distance covers only "
                     f"the {len(planned_route.stops)} sequenced stop(s)",
                     tuple(u.parcel_id for u in planned_route.unsequenced))
    else:
        warn(WARNING_NO_PLANNED_ROUTE, "no planned route is available; distance pay is 0.00")

    reading = ctx.odometer.get((rider_id, operating_date))
    actual_km = None if reading is None else (reading.end_km - reading.start_km)
    selected_km: Decimal | None = None
    source = DISTANCE_SOURCE_NONE
    flag = False
    if planned_km is not None:
        if actual_km is None:
            selected_km, source = planned_km, DISTANCE_SOURCE_PLANNED
            warn(WARNING_MISSING_ODOMETER, "no odometer reading; planned distance is used")
        elif actual_km <= planned_km * ODOMETER_TOLERANCE:       # exactly +10% is accepted
            selected_km, source = actual_km, DISTANCE_SOURCE_ODOMETER
        else:
            selected_km, source, flag = planned_km, DISTANCE_SOURCE_PLANNED, True
            warn(WARNING_ODOMETER_DISCREPANCY,
                 f"odometer {actual_km} km is more than 10% above planned {planned_km} km; planned distance is used")
    elif actual_km is None:
        warn(WARNING_MISSING_ODOMETER, "no odometer reading")

    ot_km = Decimal(0)
    if selected_km is not None and ot_planned > 0 and planned_km:
        ot_km = ot_planned if selected_km == planned_km else \
            (selected_km * ot_planned / planned_km).quantize(KM_QUANTUM, rounding=ROUND_HALF_UP)
        ot_km = min(ot_km, selected_km)
    regular_km = (selected_km - ot_km) if selected_km is not None else Decimal(0)
    overtime_rate = DISTANCE_RATE_PER_KM * OVERTIME_MULTIPLIER
    regular_pay = _q(regular_km * DISTANCE_RATE_PER_KM)
    overtime_pay = _q(ot_km * overtime_rate)
    if selected_km is not None:
        items.append(LineItem(CODE_DISTANCE_REGULAR, EARNING,
                              f"regular distance ({source.lower()} basis) at {DISTANCE_RATE_PER_KM}/km",
                              regular_pay, regular_km, DISTANCE_RATE_PER_KM))
        if ot_km:
            items.append(LineItem(CODE_DISTANCE_OVERTIME, EARNING,
                                  f"overtime distance at {DISTANCE_RATE_PER_KM} x {OVERTIME_MULTIPLIER} = {overtime_rate}/km",
                                  overtime_pay, ot_km, overtime_rate))

    # incentive lines
    if volume:
        items.append(LineItem(CODE_VOLUME_INCENTIVE, EARNING, f"volume incentive for {delivered} delivered parcels",
                              volume, Decimal(delivered)))
    if fa_incentive:
        items.append(LineItem(CODE_FIRST_ATTEMPT_INCENTIVE, EARNING,
                              f"first-attempt success {first_ok}/{denominator} = {rate_pct}% (>= 95%)",
                              fa_incentive, Decimal(first_ok)))

    gross = delivery_pay + pay_failed + regular_pay + overtime_pay + volume + fa_incentive

    # ---- BR-08 penalties ----
    n_fault, n_late, n_complaint = len(fault_refs), len(late_refs), len(complaints)
    pen_fault = RIDER_FAULT_PENALTY * n_fault
    pen_late = LATE_DELIVERY_PENALTY * n_late
    pen_complaint = COMPLAINT_PENALTY * n_complaint
    uncapped = pen_fault + pen_late + pen_complaint
    cap = _q(gross * PENALTY_CAP_RATE, ROUND_DOWN)
    deducted = min(uncapped, cap)
    for refs, amt, unit, code, text in (
            (fault_refs, pen_fault, RIDER_FAULT_PENALTY, CODE_PENALTY_RIDER_FAULT, "RIDER_FAULT failed attempt(s)"),
            (late_refs, pen_late, LATE_DELIVERY_PENALTY, CODE_PENALTY_LATE, "delivery/deliveries after window_end"),
            ([c.event_id for c in complaints], pen_complaint, COMPLAINT_PENALTY, CODE_PENALTY_COMPLAINT, "complaint(s)")):
        if refs:
            items.append(LineItem(code, DEDUCTION, f"{len(refs)} {text} at {unit} each (before cap)",
                                  -amt, Decimal(len(refs)), unit, tuple(refs)))
    if uncapped > deducted:
        items.append(LineItem(CODE_PENALTY_CAP_RELIEF, ADJUSTMENT,
                              f"penalties capped at 25% of gross ({cap}); uncapped {uncapped}",
                              uncapped - deducted))

    # ---- BR-09 COD ----
    deposits = ctx.deposits.get((rider_id, operating_date), ())
    deadline = datetime.combine(operating_date, COD_DEADLINE)
    on_time = sum((d.amount for d in deposits if d.deposit_ts <= deadline), Decimal(0))
    late = sum((d.amount for d in deposits if d.deposit_ts > deadline), Decimal(0))
    credited = on_time + (late if policy.late_deposit_treatment == LATE_COUNT_AND_FLAG else Decimal(0))
    shortfall = max(Decimal(0), cod_collected - credited)
    surplus = max(Decimal(0), credited - cod_collected)
    freeze = shortfall > FREEZE_SHORTFALL_THRESHOLD
    deposit_on_time = None if not deposits else all(d.deposit_ts <= deadline for d in deposits)
    if shortfall:
        items.append(LineItem(CODE_COD_SHORTFALL, DEDUCTION,
                              f"COD collected {cod_collected} - credited deposits {credited}", -shortfall))
    if late:
        warn(WARNING_LATE_DEPOSIT,
             f"deposit(s) after 21:00:00 totalling {late}; "
             + ("counted toward reconciliation but not as on-time"
                if policy.late_deposit_treatment == LATE_COUNT_AND_FLAG else "not credited, so left in the shortfall"),
             tuple(d.deposit_id for d in deposits if d.deposit_ts > deadline))
    if cod_collected > 0 and not deposits:
        warn(WARNING_NO_DEPOSIT, f"COD {cod_collected} collected but no deposit recorded")
    if surplus:
        warn(WARNING_COD_SURPLUS, f"deposits exceed COD collected by {surplus}; no deduction and no credit")
    if freeze:
        warn(WARNING_WEEKLY_FREEZE, f"shortfall {shortfall} is above {FREEZE_SHORTFALL_THRESHOLD}; weekly payout freeze flag set")

    net = gross - deducted - shortfall
    if net < 0:
        warn(WARNING_NEGATIVE_NET, f"daily net payout is negative ({net}); it is reported as is")

    return DailyPayoutResult(
        rider_id=rider_id, operating_date=operating_date, status=STATUS_REVIEW if warns else STATUS_OK,
        delivered_count=delivered, standard_delivered_count=n_std, express_delivered_count=n_exp,
        failed_paid_count=n_fail_paid, failed_unpaid_count=n_fail_unpaid, rescheduled_count=n_resched,
        first_attempt_successes=first_ok, eligible_attempt_count=denominator,
        first_attempt_success_rate_pct=rate_pct,
        delivery_pay=delivery_pay, delivery_pay_standard=pay_std, delivery_pay_express=pay_exp,
        failed_attempt_pay=pay_failed,
        planned_distance_km=planned_km, odometer_distance_km=actual_km, selected_distance_km=selected_km,
        distance_source=source, odometer_discrepancy_flag=flag,
        regular_distance_km=regular_km, overtime_distance_km=ot_km,
        regular_rate_per_km=DISTANCE_RATE_PER_KM, overtime_rate_per_km=overtime_rate,
        regular_distance_pay=regular_pay, overtime_distance_pay=overtime_pay,
        volume_incentive=volume, first_attempt_incentive=fa_incentive,
        rider_fault_count=n_fault, late_delivery_count=n_late, complaint_count=n_complaint,
        rider_fault_penalty=pen_fault, late_delivery_penalty=pen_late, complaint_penalty=pen_complaint,
        penalties_uncapped=uncapped, penalty_cap=cap, penalties_deducted=deducted,
        cod_collected=cod_collected, cod_deposited_total=on_time + late, cod_deposited_on_time=on_time,
        cod_deposited_late=late, cod_credited=credited, cod_surplus=surplus, cod_shortfall=shortfall,
        deposit_count=len(deposits), last_deposit_ts=max((d.deposit_ts for d in deposits), default=None),
        deposit_on_time=deposit_on_time, weekly_freeze_flag=freeze,
        gross_earnings=gross, total_deductions=deducted + shortfall, net_payout=net,
        line_items=tuple(items) + tuple(warns), warnings=tuple(warns),
    )


def riders_for_date(ctx: PayoutContext, operating_date: date) -> tuple[str, ...]:
    """Riders who worked: a roster shift, or any event, odometer row or deposit on the date."""
    _require_date(operating_date, "operating_date")
    ids = {rid for (rid, d) in ctx.shifts if d == operating_date}
    ids |= {rid for (rid, d) in ctx.odometer if d == operating_date}
    ids |= {rid for (rid, d) in ctx.deposits if d == operating_date}
    ids |= {e.rider_id for e in ctx.event_log.query(event_date=operating_date)}
    return tuple(sorted(ids))


def calculate_daily_payouts(ctx: PayoutContext, operating_date: date, *,
                            rider_ids: Iterable[str] | None = None,
                            planned_routes: Mapping[str, RouteResult] | None = None,
                            policy: PayoutPolicy = DEFAULT_POLICY) -> tuple[DailyPayoutResult, ...]:
    """Daily payouts for a batch of riders (default: ``riders_for_date``), sorted by rider_id.

    planned_routes: None -> every rider's route is derived from the events; a
    mapping (e.g. {a.rider_id: a.route for a in assign_parcels(...).assignments})
    is authoritative: a rider missing from it has no planned route.
    """
    ids = sorted(set(riders_for_date(ctx, operating_date) if rider_ids is None else rider_ids))
    out = []
    for rid in ids:
        if planned_routes is None:
            out.append(calculate_daily_payout(ctx, rid, operating_date, policy=policy))
        else:
            out.append(calculate_daily_payout(ctx, rid, operating_date, policy=policy,
                                              planned_route=planned_routes.get(rid)))
    return tuple(out)
