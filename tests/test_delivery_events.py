"""Tests for ps035.delivery_events (event recording + BR-11 parcel lifecycle).

Small tests build parcels / riders in memory (the loader's own record types);
the "real sample" tests use the official data/ pack, read-only.
"""
import hashlib
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest

from ps035.delivery_events import (
    COMPLAINT, DELIVERED, EVENT_TYPES, EXPRESS, FAILED, FAILURE_REASONS,
    FLAG_UNRECOGNISED_REASON, RESCHEDULED, STANDARD, STATUS_DELIVERED,
    STATUS_FAILED, STATUS_PENDING, STATUS_RESCHEDULED, STATUS_RTO,
    EventLog, ParcelLifecycle, load_event_log,
)
from ps035.loader import DataValidationError, DeliveryEvent, Parcel, Rider, load_dataset

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
D = date(2026, 10, 12)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def parcel(pid="PCL-00001", pool=D, priority=STANDARD, cod="0.00"):
    amount = Decimal(cod)
    return Parcel(pid, pool, Decimal("12.9"), Decimal("77.6"), Decimal("1.0"),
                  time(9, 0), time(12, 0), priority, amount > 0, amount)


def rider(rid):
    return Rider(rid, "Name", "BIKE", 40, Decimal("25.0"), "HUB-01", time(9, 0), time(18, 0))


def make_log(*parcels, events=()):
    ps = parcels or (parcel(),)
    return EventLog(ps, [rider("RDR-01"), rider("RDR-02")], events)


def rec(log, ts, etype, *, pid="PCL-00001", rid="RDR-01", **kw):
    return log.record_event(parcel_id=pid, rider_id=rid, event_ts=ts, event_type=etype, **kw)


def messages(exc_info):
    return " | ".join(i.message for i in exc_info.value.issues)


def rejects(log, ts, etype, fragment, **kw):
    n = len(log)
    with pytest.raises(DataValidationError) as exc:
        rec(log, ts, etype, **kw)
    assert fragment in messages(exc), messages(exc)
    assert len(log) == n  # nothing recorded on failure


@pytest.fixture
def log():
    return make_log(parcel(cod="500.00"))


@pytest.fixture(scope="module")
def dataset():
    return load_dataset(DATA)


@pytest.fixture(scope="module")
def sample_log(dataset):
    return EventLog.from_dataset(dataset)


# --------------------------------------------------------------------------
# 1. each supported event type
# --------------------------------------------------------------------------

def test_event_type_constants_match_project_schema():
    assert set(EVENT_TYPES) == {"DELIVERED", "FAILED", "RESCHEDULED", "COMPLAINT"}
    assert FAILURE_REASONS == ("NOT_HOME", "WRONG_ADDRESS", "RIDER_FAULT")


def test_record_delivered(log):
    e = rec(log, "2026-10-12 10:00:00", DELIVERED, cod_collected="500.00")
    assert isinstance(e, DeliveryEvent)
    assert (e.event_type, e.attempt_no, e.cod_collected) == (DELIVERED, 1, Decimal("500.00"))
    assert e.event_date == D and e.event_ts == datetime(2026, 10, 12, 10, 0, 0)
    assert log.lifecycle("PCL-00001").status == STATUS_DELIVERED


def test_record_failed(log):
    e = rec(log, "2026-10-12 10:00:00", FAILED, reason_code="NOT_HOME")
    assert e.reason_code == "NOT_HOME" and e.cod_collected == Decimal("0.00")
    lc = log.lifecycle("PCL-00001")
    assert lc.status == STATUS_FAILED and lc.reschedule_count == 0


def test_record_rescheduled_is_not_delivered(log):
    rec(log, "2026-10-12 10:00:00", RESCHEDULED)
    lc = log.lifecycle("PCL-00001")
    assert lc.status == STATUS_RESCHEDULED and not lc.is_delivered


def test_record_complaint_is_history_only(log):
    rec(log, "2026-10-12 10:00:00", DELIVERED, cod_collected="500.00")
    rec(log, "2026-10-12 11:00:00", COMPLAINT, reason_code="RUDE_BEHAVIOUR")
    assert log.lifecycle("PCL-00001").status == STATUS_DELIVERED  # unchanged
    assert [e.event_type for e in log.events_for_parcel("PCL-00001")] == [DELIVERED, COMPLAINT]
    rejects(log, "2026-10-12 12:00:00", COMPLAINT, "free-text reason_code", reason_code="  ")


def test_no_events_means_pending():
    lc = make_log().lifecycle("PCL-00001")
    assert lc.status == STATUS_PENDING and lc.reschedule_count == 0 and lc.current_priority == STANDARD


# --------------------------------------------------------------------------
# 2-3. unknown parcel / rider, invalid type
# --------------------------------------------------------------------------

def test_unknown_parcel(log):
    rejects(log, "2026-10-12 10:00:00", DELIVERED, "parcel_id 'PCL-99999' not found", pid="PCL-99999")


def test_unknown_rider(log):
    rejects(log, "2026-10-12 10:00:00", DELIVERED, "rider_id 'RDR-99' not found", rid="RDR-99")


@pytest.mark.parametrize("bad", ["RETURNED", "delivered", "", "DELIVERED ", None])
def test_invalid_event_type(log, bad):
    rejects(log, "2026-10-12 10:00:00", bad, "event_type")


def test_all_problems_are_reported_together(log):
    with pytest.raises(DataValidationError) as exc:
        rec(log, "2026-10-12 10:00:00", "NOPE", pid="PCL-99999", rid="RDR-99")
    assert len(exc.value.issues) == 3


# --------------------------------------------------------------------------
# 4. failure reasons
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reason", FAILURE_REASONS)
def test_each_documented_failure_reason_is_accepted(reason):
    assert rec(make_log(), "2026-10-12 10:00:00", FAILED, reason_code=reason).reason_code == reason


@pytest.mark.parametrize("reason", ["", "CUSTOMER_BUSY", "not_home", "OTHER"])
def test_failed_needs_a_documented_reason(log, reason):
    rejects(log, "2026-10-12 10:00:00", FAILED, "reason_code")


# --------------------------------------------------------------------------
# 5. COD
# --------------------------------------------------------------------------

def test_cod_absent_is_zero(log):
    assert rec(log, "2026-10-12 10:00:00", DELIVERED).cod_collected == Decimal("0.00")


def test_cod_zero_is_valid():
    log = make_log(parcel(cod="500.00"))
    assert rec(log, "2026-10-12 10:00:00", DELIVERED, cod_collected="0.00").cod_collected == 0


@pytest.mark.parametrize("amount", ["0.01", "250.50", "499.99", "500.00", Decimal("500"), 500])
def test_cod_partial_or_full_is_valid(amount):
    log = make_log(parcel(cod="500.00"))
    assert rec(log, "2026-10-12 10:00:00", DELIVERED, cod_collected=amount).cod_collected == Decimal(str(amount))


def test_cod_is_decimal():
    e = rec(make_log(parcel(cod="500.00")), "2026-10-12 10:00:00", DELIVERED, cod_collected="0.10")
    assert isinstance(e.cod_collected, Decimal) and e.cod_collected + Decimal("0.20") == Decimal("0.30")


@pytest.mark.parametrize("amount,fragment", [
    ("-1.00", "must not be negative"),
    ("abc", "is not a number"),
    ("NaN", "not a finite"),
    ("Infinity", "not a finite"),
    ("500.01", "exceeds the parcel's cod_amount"),
    ("1000", "exceeds"),
    ("10.005", "more than 2 decimal places"),
    (12.5, "not float"),
    (True, "not bool"),
    ([1], "must be a Decimal"),
])
def test_cod_invalid(log, amount, fragment):
    rejects(log, "2026-10-12 10:00:00", DELIVERED, fragment, cod_collected=amount)


def test_cod_on_non_cod_parcel_must_be_zero():
    log = make_log(parcel(cod="0.00"))
    rejects(log, "2026-10-12 10:00:00", DELIVERED, "exceeds the parcel's cod_amount", cod_collected="1.00")


@pytest.mark.parametrize("etype,kw", [(FAILED, {"reason_code": "NOT_HOME"}), (RESCHEDULED, {})])
def test_cod_collected_on_non_delivered_event_is_contradictory(etype, kw):
    log = make_log(parcel(cod="500.00"))
    rejects(log, "2026-10-12 10:00:00", etype, "cod_collected must be 0", cod_collected="10.00", **kw)


# --------------------------------------------------------------------------
# 6. history preservation / deterministic ordering
# --------------------------------------------------------------------------

def test_history_is_preserved_and_append_only():
    log = make_log(parcel(), parcel("PCL-00002"))
    first = rec(log, "2026-10-12 10:00:00", FAILED, reason_code="NOT_HOME")
    before = log.events
    second = rec(log, "2026-10-12 11:00:00", RESCHEDULED)
    assert log.events[:1] == before and log.events == (first, second)
    assert first.reason_code == "NOT_HOME" and first.event_type == FAILED  # untouched


def test_loaded_records_are_not_mutated():
    p = parcel()
    log = make_log(p)
    rec(log, "2026-10-12 10:00:00", RESCHEDULED)
    assert p.priority == STANDARD  # Parcel is frozen; lifecycle is derived, not stored
    assert log.lifecycle("PCL-00001").current_priority == EXPRESS


def test_query_order_is_deterministic_ts_then_event_id():
    log = make_log(parcel(), parcel("PCL-00002"), parcel("PCL-00003"))
    rec(log, "2026-10-12 10:00:00", DELIVERED, pid="PCL-00002", event_id="EVT-00009")
    rec(log, "2026-10-12 10:00:00", DELIVERED, pid="PCL-00001", event_id="EVT-00002")  # same second, lower id
    rec(log, "2026-10-12 09:00:00", DELIVERED, pid="PCL-00003", event_id="EVT-00100")  # earlier time wins
    ids = [e.event_id for e in log.events]
    assert ids == ["EVT-00100", "EVT-00002", "EVT-00009"]
    assert [e.event_id for e in log.query(event_date=D)] == ids
    assert [e.event_id for e in log.query(rider_id="RDR-01")] == ids
    assert log.events == log.events  # repeatable


def test_queries_by_parcel_rider_date_and_type():
    log = make_log(parcel(), parcel("PCL-00002"))
    a = rec(log, "2026-10-12 10:00:00", RESCHEDULED, pid="PCL-00001", rid="RDR-01")
    b = rec(log, "2026-10-13 10:00:00", DELIVERED, pid="PCL-00001", rid="RDR-02")
    c = rec(log, "2026-10-12 12:00:00", FAILED, pid="PCL-00002", rid="RDR-01", reason_code="RIDER_FAULT")
    assert log.events_for_parcel("PCL-00001") == (a, b)
    assert log.events_for_parcel("PCL-00099") == ()
    assert log.events_for_rider("RDR-01") == (a, c)
    assert log.events_for_rider("RDR-01", date(2026, 10, 13)) == ()
    assert log.events_for_rider("RDR-02", date(2026, 10, 13)) == (b,)
    assert log.query(rider_id="RDR-01", event_date=D, event_types=[FAILED]) == (c,)
    assert log.query(parcel_id="PCL-00001", rider_id="RDR-01") == (a,)
    assert log.query() == (a, c, b)


def test_event_ids_are_generated_in_sequence_and_never_reused():
    log = make_log()
    e1 = rec(log, "2026-10-12 10:00:00", FAILED, reason_code="NOT_HOME")
    e2 = rec(log, "2026-10-12 11:00:00", RESCHEDULED)
    assert (e1.event_id, e2.event_id) == ("EVT-00001", "EVT-00002")
    rejects(log, "2026-10-13 10:00:00", DELIVERED, "duplicate event_id", event_id="EVT-00001")
    rejects(log, "2026-10-13 10:00:00", DELIVERED, "bad event_id", event_id="X-1")


# --------------------------------------------------------------------------
# 7. legitimate repeated attempts vs true duplicates
# --------------------------------------------------------------------------

def test_failed_then_rescheduled_then_delivered_is_legitimate():
    log = make_log()
    rec(log, "2026-10-12 10:00:00", FAILED, reason_code="NOT_HOME")
    rec(log, "2026-10-12 10:05:00", RESCHEDULED)
    e = rec(log, "2026-10-13 10:00:00", DELIVERED)
    assert e.attempt_no == 2
    assert [x.event_type for x in log.events_for_parcel("PCL-00001")] == [FAILED, RESCHEDULED, DELIVERED]


def test_second_attempt_may_fail_again_without_being_a_duplicate():
    log = make_log()
    rec(log, "2026-10-12 10:00:00", FAILED, reason_code="NOT_HOME")
    rec(log, "2026-10-12 10:05:00", RESCHEDULED)
    e = rec(log, "2026-10-13 10:00:00", FAILED, reason_code="NOT_HOME")  # same reason, new attempt
    assert e.attempt_no == 2 and log.lifecycle("PCL-00001").status == STATUS_FAILED


def test_true_duplicates_are_rejected():
    log = make_log()
    rec(log, "2026-10-12 10:00:00", FAILED, reason_code="NOT_HOME")
    rejects(log, "2026-10-12 10:00:00", FAILED, "duplicate outcome", reason_code="NOT_HOME")
    rejects(log, "2026-10-12 12:00:00", FAILED, "duplicate outcome", reason_code="RIDER_FAULT")
    rejects(log, "2026-10-12 12:00:00", DELIVERED, "duplicate outcome")


def test_same_event_replayed_after_reschedule_is_a_duplicate_reschedule():
    log = make_log()
    rec(log, "2026-10-12 10:00:00", RESCHEDULED)
    rejects(log, "2026-10-12 10:00:00", RESCHEDULED, "before the parcel's next eligible date")


def test_delivered_parcel_cannot_be_delivered_again():
    log = make_log()
    rec(log, "2026-10-12 10:00:00", DELIVERED)
    rejects(log, "2026-10-13 10:00:00", DELIVERED, "after DELIVERED")
    rejects(log, "2026-10-13 10:00:00", FAILED, "after DELIVERED", reason_code="NOT_HOME")
    rejects(log, "2026-10-13 10:00:00", RESCHEDULED, "after DELIVERED")


def test_different_parcels_may_share_timestamp_and_rider():
    log = make_log(parcel(), parcel("PCL-00002"))
    rec(log, "2026-10-12 10:00:00", DELIVERED, pid="PCL-00001")
    rec(log, "2026-10-12 10:00:00", DELIVERED, pid="PCL-00002")
    assert len(log) == 2


def test_attempt_no_is_derived_and_must_match_if_given():
    log = make_log()
    rejects(log, "2026-10-12 10:00:00", DELIVERED, "attempt_no 2 should be 1", attempt_no=2)
    rejects(log, "2026-10-12 10:00:00", DELIVERED, "attempt_no", attempt_no=3)
    rec(log, "2026-10-12 10:00:00", RESCHEDULED, attempt_no=1)
    rejects(log, "2026-10-13 10:00:00", DELIVERED, "attempt_no 1 should be 2", attempt_no=1)


# --------------------------------------------------------------------------
# 8. events outside rider shifts
# --------------------------------------------------------------------------

@pytest.mark.parametrize("ts", ["2026-10-12 00:00:01", "2026-10-12 03:30:00", "2026-10-12 23:59:59"])
def test_events_outside_any_shift_window_are_accepted(ts):
    # Riders in make_log have a 09:00-18:00 default shift; the module never checks it.
    assert rec(make_log(), ts, DELIVERED).event_ts == datetime.fromisoformat(ts)


# --------------------------------------------------------------------------
# 9-11. BR-11: first reschedule, second reschedule (RTO), all parcels
# --------------------------------------------------------------------------

def test_first_reschedule_next_day_and_priority_upgrade():
    log = make_log()
    rec(log, "2026-10-12 16:00:00", RESCHEDULED)
    lc = log.lifecycle("PCL-00001")
    assert lc == ParcelLifecycle("PCL-00001", STANDARD, EXPRESS, 1, STATUS_RESCHEDULED,
                                 date(2026, 10, 13), 1, "EVT-00001")
    assert lc.priority_upgraded and lc.awaiting_repool and not lc.is_rto


def test_first_reschedule_pool_membership():
    log = make_log()
    assert [p.parcel_id for p in log.pool_for_date(D)] == ["PCL-00001"]
    rec(log, "2026-10-12 16:00:00", RESCHEDULED)
    assert [p.parcel_id for p in log.pool_for_date(D)] == ["PCL-00001"]            # that morning: still fresh
    nxt = log.pool_for_date(date(2026, 10, 13))
    assert [(p.parcel_id, p.priority) for p in nxt] == [("PCL-00001", EXPRESS)]    # upgraded copy
    assert log.pool_for_date(date(2026, 10, 14)) == ()                             # not beyond the next day


def test_express_parcel_stays_express():
    log = make_log(parcel(priority=EXPRESS))
    rec(log, "2026-10-12 16:00:00", RESCHEDULED)
    lc = log.lifecycle("PCL-00001")
    assert lc.current_priority == EXPRESS and lc.reschedule_count == 1 and not lc.priority_upgraded


def test_second_reschedule_makes_rto_and_leaves_pool():
    log = make_log()
    rec(log, "2026-10-12 16:00:00", RESCHEDULED)
    rec(log, "2026-10-13 16:00:00", RESCHEDULED)
    lc = log.lifecycle("PCL-00001")
    assert lc.is_rto and lc.status == STATUS_RTO and lc.reschedule_count == 2
    assert lc.next_eligible_date is None and lc.current_priority == EXPRESS
    assert log.pool_for_date(date(2026, 10, 13)) != ()            # it WAS in the 13th's pool
    assert log.pool_for_date(date(2026, 10, 14)) == ()            # and is gone afterwards
    assert log.rto_parcel_ids() == ("PCL-00001",)


@pytest.mark.parametrize("pid,prio", [("PCL-00010", STANDARD), ("PCL-00777", STANDARD), ("PCL-12345", EXPRESS)])
def test_rto_rule_applies_to_any_parcel(pid, prio):
    other = parcel("PCL-00002")
    log = make_log(parcel(pid, priority=prio), other)
    rec(log, "2026-10-12 09:00:00", RESCHEDULED, pid=pid)
    rec(log, "2026-10-13 09:00:00", RESCHEDULED, pid=pid)
    assert log.lifecycle(pid).is_rto
    assert not log.lifecycle("PCL-00002").is_rto
    assert log.rto_parcel_ids() == (pid,)


def test_one_reschedule_then_delivered_is_not_rto():
    log = make_log()
    rec(log, "2026-10-12 09:00:00", RESCHEDULED)
    rec(log, "2026-10-13 09:00:00", DELIVERED)
    lc = log.lifecycle("PCL-00001")
    assert lc.status == STATUS_DELIVERED and lc.reschedule_count == 1 and lc.current_priority == EXPRESS
    assert lc.next_eligible_date is None


def test_lifecycle_as_of_start_of_day():
    log = make_log()
    rec(log, "2026-10-12 09:00:00", RESCHEDULED)
    rec(log, "2026-10-13 09:00:00", RESCHEDULED)
    assert log.lifecycle("PCL-00001", before=date(2026, 10, 12)).status == STATUS_PENDING
    assert log.lifecycle("PCL-00001", before=date(2026, 10, 13)).status == STATUS_RESCHEDULED
    assert log.lifecycle("PCL-00001", before=date(2026, 10, 14)).status == STATUS_RTO
    with pytest.raises(KeyError):
        log.lifecycle("PCL-99999")


# --------------------------------------------------------------------------
# 12. reschedule edge cases / contradictions
# --------------------------------------------------------------------------

def test_two_reschedules_on_the_same_day_are_rejected():
    log = make_log()
    rec(log, "2026-10-12 10:00:00", RESCHEDULED)
    rejects(log, "2026-10-12 18:00:00", RESCHEDULED, "before the parcel's next eligible date 2026-10-13")
    assert log.lifecycle("PCL-00001").reschedule_count == 1


def test_events_after_rto_are_rejected_including_complaint():
    log = make_log()
    rec(log, "2026-10-12 10:00:00", RESCHEDULED)
    rec(log, "2026-10-13 10:00:00", RESCHEDULED)
    rejects(log, "2026-10-14 10:00:00", DELIVERED, "event after RTO")
    rejects(log, "2026-10-14 10:00:00", FAILED, "event after RTO", reason_code="NOT_HOME")
    rejects(log, "2026-10-14 10:00:00", RESCHEDULED, "event after RTO")
    rejects(log, "2026-10-14 10:00:00", COMPLAINT, "event after RTO", reason_code="X")
    assert len(log.events_for_parcel("PCL-00001")) == 2


def test_event_before_next_eligible_date_is_rejected():
    log = make_log()
    rec(log, "2026-10-12 10:00:00", RESCHEDULED)
    rejects(log, "2026-10-12 23:00:00", DELIVERED, "next eligible date")


def test_failed_parcel_may_be_rescheduled_but_not_redelivered_same_attempt():
    log = make_log()
    rec(log, "2026-10-12 10:00:00", FAILED, reason_code="WRONG_ADDRESS")
    rejects(log, "2026-10-12 10:30:00", DELIVERED, "duplicate outcome")
    rec(log, "2026-10-12 11:00:00", RESCHEDULED)
    assert log.lifecycle("PCL-00001").status == STATUS_RESCHEDULED


def test_backdated_event_that_breaks_existing_history_is_rejected():
    log = make_log()
    rec(log, "2026-10-13 10:00:00", DELIVERED, attempt_no=1)
    n = len(log)
    # A reschedule dated BEFORE the delivery would make the delivery attempt 2 -> existing event invalid.
    with pytest.raises(DataValidationError) as exc:
        rec(log, "2026-10-12 10:00:00", RESCHEDULED)
    assert "would make existing event" in messages(exc)
    assert len(log) == n


def test_event_before_parcel_pool_date_is_rejected():
    log = make_log(parcel(pool=date(2026, 10, 13)))
    rejects(log, "2026-10-12 23:59:59", DELIVERED, "before the parcel's pool_date")


@pytest.mark.parametrize("etype,kw,fragment", [
    (DELIVERED, {"reason_code": "NOT_HOME"}, "DELIVERED event must have an empty reason_code"),
    (RESCHEDULED, {"reason_code": "NOT_HOME"}, "RESCHEDULED event must have an empty reason_code"),
    (FAILED, {"reason_code": "NOT_HOME", "cod_collected": "5.00"}, "cod_collected must be 0"),
])
def test_contradictory_field_combinations(log, etype, kw, fragment):
    rejects(log, "2026-10-12 10:00:00", etype, fragment, **kw)


# --------------------------------------------------------------------------
# 14. date/time boundaries
# --------------------------------------------------------------------------

def test_reschedule_at_last_second_of_day_is_eligible_next_calendar_day():
    log = make_log()
    rec(log, "2026-10-12 23:59:59", RESCHEDULED)
    assert log.lifecycle("PCL-00001").next_eligible_date == date(2026, 10, 13)
    rejects(log, "2026-10-12 23:59:59", DELIVERED, "next eligible date")     # same instant, same day
    e = rec(log, "2026-10-13 00:00:00", DELIVERED)                           # first second of next day
    assert e.event_date == date(2026, 10, 13) and e.attempt_no == 2


def test_next_day_rolls_over_month_end():
    log = make_log(parcel(pool=date(2026, 10, 31)))
    rec(log, "2026-10-31 12:00:00", RESCHEDULED)
    assert log.lifecycle("PCL-00001").next_eligible_date == date(2026, 11, 1)


@pytest.mark.parametrize("ts", ["2026-10-12", "2026-10-12 10:00", "2026/10/12 10:00:00",
                                "2026-10-12T10:00:00", "2026-10-12 24:00:00", "2026-02-30 10:00:00", 12345])
def test_bad_timestamp_formats_are_rejected(log, ts):
    rejects(log, ts, DELIVERED, "event_ts")


def test_timezone_aware_timestamp_rejected(log):
    from datetime import timezone
    rejects(log, datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc), DELIVERED, "timezone-naive")


def test_event_date_must_match_timestamp_date(log):
    rejects(log, "2026-10-12 10:00:00", DELIVERED, "event_ts date differs from event_date", event_date="2026-10-13")
    assert rec(log, "2026-10-12 10:00:00", DELIVERED, event_date=D).event_date == D


def test_datetime_object_and_string_are_equivalent():
    a, b = make_log(), make_log()
    ea = rec(a, datetime(2026, 10, 12, 9, 30, 0), DELIVERED)
    eb = rec(b, "2026-10-12 09:30:00", DELIVERED)
    assert ea == eb


# --------------------------------------------------------------------------
# History validation (constructor)
# --------------------------------------------------------------------------

def _ev(eid, ts, etype, pid="PCL-00001", rid="RDR-01", reason="", att=1, cod="0.00"):
    t = datetime.fromisoformat(ts)
    return DeliveryEvent(eid, t.date(), pid, rid, t, etype, reason, att, Decimal(cod))


def test_history_with_contradictions_reports_every_problem_with_row_numbers():
    events = [
        _ev("EVT-00001", "2026-10-12 10:00:00", DELIVERED),
        _ev("EVT-00002", "2026-10-12 11:00:00", DELIVERED),               # duplicate delivery
        _ev("EVT-00003", "2026-10-12 11:00:00", DELIVERED, pid="PCL-99999"),
        _ev("EVT-00004", "2026-10-12 11:00:00", DELIVERED, pid="PCL-00002", rid="RDR-99"),
    ]
    with pytest.raises(DataValidationError) as exc:
        make_log(parcel(), parcel("PCL-00002"), events=events)
    text = [str(i) for i in exc.value.issues]
    assert any("row 2" in t and "after DELIVERED" in t for t in text)
    assert any("row 3" in t and "PCL-99999" in t for t in text)
    assert any("row 4" in t and "RDR-99" in t for t in text)


def test_history_duplicate_event_id_is_rejected():
    events = [_ev("EVT-00001", "2026-10-12 10:00:00", DELIVERED),
              _ev("EVT-00001", "2026-10-12 11:00:00", DELIVERED, pid="PCL-00002")]
    with pytest.raises(DataValidationError) as exc:
        make_log(parcel(), parcel("PCL-00002"), events=events)
    assert "duplicate event_id" in messages(exc)


def test_history_accepts_unlisted_failure_reason_but_flags_it():
    ev = _ev("EVT-00001", "2026-10-12 10:00:00", FAILED, reason="CUSTOMER_BUSY")
    log = make_log(events=[ev])
    assert log.flagged_events() == ((ev, FLAG_UNRECOGNISED_REASON),)
    assert log.lifecycle("PCL-00001").status == STATUS_FAILED
    # ...but a NEW event with that reason is refused
    rejects(log, "2026-10-12 12:00:00", FAILED, "reason_code", pid="PCL-00001", reason_code="CUSTOMER_BUSY")


def test_history_input_order_does_not_matter():
    a = _ev("EVT-00001", "2026-10-12 10:00:00", RESCHEDULED)
    b = _ev("EVT-00002", "2026-10-13 10:00:00", DELIVERED, att=2)
    assert make_log(events=[b, a]).events == make_log(events=[a, b]).events == (a, b)


# --------------------------------------------------------------------------
# 13. the real sample data
# --------------------------------------------------------------------------

def test_sample_history_loads_and_is_preserved_exactly(dataset, sample_log):
    assert len(sample_log) == 1530
    assert set(sample_log.events) == set(dataset.events)          # nothing added, dropped or altered
    assert sample_log.events == tuple(sorted(dataset.events, key=lambda e: (e.event_ts, e.event_id)))
    assert load_event_log(DATA).events == sample_log.events


def test_sample_events_by_type(sample_log):
    counts = {t: len(sample_log.query(event_types=[t])) for t in EVENT_TYPES}
    assert counts == {DELIVERED: 1442, FAILED: 53, RESCHEDULED: 32, COMPLAINT: 3}


def test_four_parcels_rescheduled_twice_all_become_rto(sample_log):
    expected = ("PCL-00435", "PCL-00651", "PCL-00820", "PCL-00828")
    assert sample_log.rto_parcel_ids() == expected
    for pid in expected:
        lc = sample_log.lifecycle(pid)
        assert lc.is_rto and lc.reschedule_count == 2 and lc.next_eligible_date is None
        assert lc.current_priority == EXPRESS and lc.original_priority == STANDARD
    # the rule is derived from events: exactly the parcels with 2 RESCHEDULED events
    twice = {pid for pid in sample_log.all_lifecycles()
             if len(sample_log.query(parcel_id=pid, event_types=[RESCHEDULED])) == 2}
    assert twice == set(expected)


def test_pcl_00435_lifecycle_dates(sample_log):
    assert [e.event_id for e in sample_log.events_for_parcel("PCL-00435")] == ["EVT-00417", "EVT-00625"]
    assert sample_log.lifecycle("PCL-00435", before=date(2026, 10, 14)).next_eligible_date == date(2026, 10, 14)
    ids14 = {p.parcel_id: p.priority for p in sample_log.pool_for_date(date(2026, 10, 14))}
    ids15 = {p.parcel_id for p in sample_log.pool_for_date(date(2026, 10, 15))}
    assert ids14["PCL-00435"] == EXPRESS and "PCL-00435" not in ids15


def test_rescheduled_parcels_return_next_day_with_upgraded_priority(sample_log):
    for e in sample_log.query(event_types=[RESCHEDULED]):
        lc = sample_log.lifecycle(e.parcel_id, before=date.fromordinal(e.event_date.toordinal() + 1))
        pool = {p.parcel_id: p.priority for p in sample_log.pool_for_date(date.fromordinal(e.event_date.toordinal() + 1))}
        if lc.status == STATUS_RESCHEDULED:
            assert pool[e.parcel_id] == EXPRESS
            assert lc.reschedule_count == 1
        else:
            assert lc.is_rto and e.parcel_id not in pool
        assert lc.reschedule_count >= 1


def test_sample_pool_each_day_matches_fresh_plus_repooled(dataset, sample_log):
    for day in sorted({p.pool_date for p in dataset.parcels}):
        fresh = {p.parcel_id for p in dataset.parcels if p.pool_date == day}
        pool = {p.parcel_id for p in sample_log.pool_for_date(day)}
        assert fresh <= pool
        repooled = pool - fresh
        for pid in repooled:
            assert sample_log.lifecycle(pid, before=day).reschedule_count == 1


def test_307_sample_events_outside_shift_windows_are_accepted(dataset, sample_log):
    shifts = {(s.rider_id, s.shift_date): s for s in dataset.shifts}
    outside = [e for e in sample_log.events
               if not (shifts[(e.rider_id, e.event_date)].shift_start <= e.event_ts.time()
                       <= shifts[(e.rider_id, e.event_date)].shift_end)]
    assert len(outside) == 307  # all of them are in the validated log


def test_customer_busy_is_loaded_flagged_and_not_reclassified(sample_log):
    flagged = sample_log.flagged_events()
    assert [(e.event_id, e.reason_code, f) for e, f in flagged] == [("EVT-00199", "CUSTOMER_BUSY", FLAG_UNRECOGNISED_REASON)]
    assert sample_log.events_for_parcel(flagged[0][0].parcel_id)[0].reason_code in ("CUSTOMER_BUSY", "")


def test_complaints_do_not_change_parcel_status(sample_log):
    for c in sample_log.query(event_types=[COMPLAINT]):
        assert sample_log.lifecycle(c.parcel_id).status == STATUS_DELIVERED


def test_sample_cod_never_exceeds_parcel_cod(dataset, sample_log):
    cod = {p.parcel_id: p.cod_amount for p in dataset.parcels}
    assert all(e.cod_collected <= cod[e.parcel_id] for e in sample_log.events)


def test_recording_on_sample_log_does_not_alter_history_or_csv_files(dataset):
    before = {p.name: p.read_bytes() for p in DATA.glob("*")}
    log = EventLog.from_dataset(dataset)
    history = log.events
    new = log.record_event(parcel_id="PCL-00001", rider_id="RDR-01", event_ts="2026-10-12 23:00:00",
                           event_type=COMPLAINT, reason_code="LATE")
    assert new.event_id == "EVT-01531" and len(log) == 1531
    assert set(history) <= set(log.events)
    assert len(dataset.events) == 1530                                 # loaded DataSet untouched
    assert before == {p.name: p.read_bytes() for p in DATA.glob("*")}  # files untouched


def test_rto_parcel_from_sample_rejects_new_events(dataset):
    log = EventLog.from_dataset(dataset)
    rejects(log, "2026-10-17 10:00:00", DELIVERED, "event after RTO", pid="PCL-00435", rid="RDR-05")


def test_sample_data_files_match_published_checksums():
    for line in (DATA / "SHA256SUMS.txt").read_text().splitlines():
        digest, name = line.split()
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name


# --------------------------------------------------------------------------
# 15. completed modules still importable with their public APIs
# --------------------------------------------------------------------------

def test_completed_modules_public_api_still_present():
    import ps035.assignment as assignment
    import ps035.command_parser as command_parser
    import ps035.distance as distance
    import ps035.eta as eta
    import ps035.loader as loader
    import ps035.route_sequencing as route_sequencing
    assert callable(loader.load_dataset) and hasattr(loader, "DeliveryEvent")
    assert callable(command_parser.parse_commands)
    assert callable(distance.haversine_km)
    assert callable(eta.calculate_stop_timing)
    assert callable(route_sequencing.sequence_route)
    assert hasattr(assignment, "VEHICLE_LIMITS")
