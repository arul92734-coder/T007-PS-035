import csv
from datetime import date, datetime, time
from decimal import Decimal

import pytest

from ps035.loader import DataValidationError, load_dataset
from tests.conftest import write_csv


def issues_of(data_dir):
    with pytest.raises(DataValidationError) as exc:
        load_dataset(data_dir)
    return [str(i) for i in exc.value.issues]


def has(issues, *fragments):
    return any(all(f in i for f in fragments) for i in issues)


# ---- valid loading -------------------------------------------------------

def test_valid_data_loads_with_real_types(data_dir):
    ds = load_dataset(data_dir)
    assert (len(ds.hubs), len(ds.riders), len(ds.shifts), len(ds.parcels)) == (1, 2, 2, 2)
    assert (len(ds.events), len(ds.odometer), len(ds.cod_deposits)) == (2, 2, 1)
    p = ds.parcels[0]
    assert p.weight_kg == Decimal("4.7") and isinstance(p.weight_kg, Decimal)
    assert p.pool_date == date(2026, 10, 12) and p.window_end == time(12, 0, 0)
    assert p.cod_flag is False and ds.parcels[1].cod_flag is True
    assert ds.events[0].event_ts == datetime(2026, 10, 12, 9, 0, 20)
    assert ds.events[0].attempt_no == 1 and ds.events[0].reason_code == ""


def test_loading_does_not_modify_input_files(data_dir):
    before = {p.name: p.read_bytes() for p in data_dir.glob("*.csv")}
    load_dataset(data_dir)
    assert before == {p.name: p.read_bytes() for p in data_dir.glob("*.csv")}


def test_events_outside_shift_window_are_accepted(data_dir, edit):
    edit("delivery_events.csv", 1, "event_ts", "2026-10-12 23:30:00")
    load_dataset(data_dir)  # event log is authoritative; must not be rejected


def test_unlisted_failure_reason_is_accepted(data_dir, edit):
    edit("delivery_events.csv", 2, "reason_code", "CUSTOMER_BUSY")
    load_dataset(data_dir)  # flagged later by payout logic, not a load error


# ---- files / columns -----------------------------------------------------

def test_missing_file(data_dir):
    (data_dir / "odometer.csv").unlink()
    assert has(issues_of(data_dir), "odometer.csv", "file not found")


def test_missing_required_column(data_dir):
    rows = [["parcel_id", "pool_date", "dest_lat", "dest_lon", "window_start",
             "window_end", "priority", "cod_flag", "cod_amount"]]  # weight_kg gone
    write_csv(data_dir, "parcels.csv", rows)
    assert has(issues_of(data_dir), "parcels.csv", "missing required column", "weight_kg")


def test_wrong_field_count(data_dir):
    with open(data_dir / "hub.csv", "a", newline="") as fh:
        csv.writer(fh).writerow(["HUB-02", "Short row"])
    assert has(issues_of(data_dir), "hub.csv row 2", "expected 4 fields")


# ---- dates / timestamps / times -----------------------------------------

@pytest.mark.parametrize("bad", ["12-10-2026", "2026-13-01", "2026-10-32", "2026-1-5", ""])
def test_malformed_date(data_dir, edit, bad):
    edit("parcels.csv", 1, "pool_date", bad)
    assert has(issues_of(data_dir), "parcels.csv row 1", "pool_date")


@pytest.mark.parametrize("bad", ["2026-10-12T09:00:20", "2026-10-12 25:00:00", "2026-10-12", "2026-10-12 9:00:20"])
def test_malformed_timestamp(data_dir, edit, bad):
    edit("delivery_events.csv", 1, "event_ts", bad)
    assert has(issues_of(data_dir), "delivery_events.csv row 1", "event_ts")


def test_malformed_time(data_dir, edit):
    edit("rider_shifts.csv", 2, "shift_start", "9am")
    assert has(issues_of(data_dir), "rider_shifts.csv row 2", "shift_start")


def test_timestamp_date_must_match_event_date(data_dir, edit):
    edit("cod_deposits.csv", 1, "deposit_ts", "2026-10-13 18:06:00")
    assert has(issues_of(data_dir), "cod_deposits.csv row 1", "deposit_ts date differs")


# ---- numbers / enums / booleans -----------------------------------------

@pytest.mark.parametrize("bad", ["abc", "-1.0", "NaN", "Infinity", ""])
def test_invalid_numeric_weight(data_dir, edit, bad):
    edit("parcels.csv", 1, "weight_kg", bad)
    assert has(issues_of(data_dir), "parcels.csv row 1", "weight_kg")


def test_negative_money(data_dir, edit):
    edit("cod_deposits.csv", 1, "amount", "-5.00")
    assert has(issues_of(data_dir), "cod_deposits.csv row 1", "amount")


def test_latitude_out_of_range(data_dir, edit):
    edit("parcels.csv", 2, "dest_lat", "91.0")
    assert has(issues_of(data_dir), "parcels.csv row 2", "dest_lat")


def test_odometer_end_before_start(data_dir, edit):
    edit("odometer.csv", 1, "end_km", "100.00")
    assert has(issues_of(data_dir), "odometer.csv row 1", "end_km is less than start_km")


@pytest.mark.parametrize("fname,row,col,bad", [
    ("riders.csv", 1, "vehicle_type", "SCOOTER"),
    ("parcels.csv", 1, "priority", "URGENT"),
    ("delivery_events.csv", 1, "event_type", "LOST"),
    ("delivery_events.csv", 1, "attempt_no", "3"),
    ("parcels.csv", 1, "cod_flag", "yes"),
    ("parcels.csv", 1, "cod_flag", "True"),
])
def test_invalid_enum_or_boolean(data_dir, edit, fname, row, col, bad):
    edit(fname, row, col, bad)
    assert has(issues_of(data_dir), f"{fname} row {row}", col)


def test_cod_amount_must_be_zero_when_not_cod(data_dir, edit):
    edit("parcels.csv", 1, "cod_amount", "10.00")
    assert has(issues_of(data_dir), "parcels.csv row 1", "cod_amount must be 0.00")


def test_failed_event_needs_reason(data_dir, edit):
    edit("delivery_events.csv", 2, "reason_code", "")
    assert has(issues_of(data_dir), "delivery_events.csv row 2", "needs a reason_code")


def test_shift_end_must_be_after_start(data_dir, edit):
    edit("rider_shifts.csv", 1, "shift_end", "07:00:00")
    assert has(issues_of(data_dir), "rider_shifts.csv row 1", "shift_end must be after")


# ---- duplicate primary IDs ----------------------------------------------

@pytest.mark.parametrize("fname,col,value,label", [
    ("riders.csv", "rider_id", "RDR-01", "rider_id"),
    ("parcels.csv", "parcel_id", "PCL-00001", "parcel_id"),
    ("delivery_events.csv", "event_id", "EVT-00001", "event_id"),
])
def test_duplicate_primary_id(data_dir, edit, fname, col, value, label):
    edit(fname, 2, col, value)  # make row 2 collide with row 1
    assert has(issues_of(data_dir), f"{fname} row 2", f"duplicate {label}")


def test_duplicate_shift_for_same_rider_and_day(data_dir, edit):
    edit("rider_shifts.csv", 2, "rider_id", "RDR-01")
    assert has(issues_of(data_dir), "rider_shifts.csv row 2", "duplicate (rider_id, shift_date)")


# ---- foreign keys --------------------------------------------------------

@pytest.mark.parametrize("fname,row,col,bad,target", [
    ("riders.csv", 1, "home_hub", "HUB-99", "hub.csv"),
    ("rider_shifts.csv", 1, "rider_id", "RDR-99", "riders.csv"),
    ("delivery_events.csv", 1, "rider_id", "RDR-99", "riders.csv"),
    ("delivery_events.csv", 1, "parcel_id", "PCL-99999", "parcels.csv"),
    ("odometer.csv", 1, "rider_id", "RDR-99", "riders.csv"),
    ("cod_deposits.csv", 1, "rider_id", "RDR-99", "riders.csv"),
])
def test_invalid_foreign_key(data_dir, edit, fname, row, col, bad, target):
    edit(fname, row, col, bad)
    assert has(issues_of(data_dir), f"{fname} row {row}", col, target)


# ---- reporting ------------------------------------------------------------

def test_all_problems_reported_together(data_dir, edit):
    edit("parcels.csv", 1, "weight_kg", "oops")
    edit("odometer.csv", 1, "rider_id", "RDR-99")
    edit("riders.csv", 1, "vehicle_type", "SCOOTER")
    assert len(issues_of(data_dir)) >= 3
