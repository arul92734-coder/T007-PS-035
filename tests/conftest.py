"""Shared test helpers: a tiny, valid data folder that tests then break on purpose."""
import csv
from pathlib import Path

import pytest

TINY = {
    "hub.csv": [
        ["hub_id", "hub_name", "lat", "lon"],
        ["HUB-01", "Test Hub", "12.903000", "77.624000"],
    ],
    "riders.csv": [
        ["rider_id", "rider_name", "vehicle_type", "capacity_parcels", "capacity_kg",
         "home_hub", "default_shift_start", "default_shift_end"],
        ["RDR-01", "Van Rider", "VAN", "120", "300.0", "HUB-01", "08:00:00", "17:00:00"],
        ["RDR-02", "Bike Rider", "BIKE", "40", "25.0", "HUB-01", "09:00:00", "18:00:00"],
    ],
    "rider_shifts.csv": [
        ["rider_id", "shift_date", "shift_start", "shift_end"],
        ["RDR-01", "2026-10-12", "08:00:00", "17:00:00"],
        ["RDR-02", "2026-10-12", "09:00:00", "18:00:00"],
    ],
    "parcels.csv": [
        ["parcel_id", "pool_date", "dest_lat", "dest_lon", "weight_kg", "window_start",
         "window_end", "priority", "cod_flag", "cod_amount"],
        ["PCL-00001", "2026-10-12", "12.918566", "77.614755", "4.7", "09:00:00", "12:00:00",
         "EXPRESS", "false", "0.00"],
        ["PCL-00002", "2026-10-12", "12.865756", "77.580447", "6.2", "09:00:00", "12:00:00",
         "STANDARD", "true", "449.00"],
    ],
    "delivery_events.csv": [
        ["event_id", "event_date", "parcel_id", "rider_id", "event_ts", "event_type",
         "reason_code", "attempt_no", "cod_collected"],
        ["EVT-00001", "2026-10-12", "PCL-00001", "RDR-01", "2026-10-12 09:00:20", "DELIVERED", "", "1", "0.00"],
        ["EVT-00002", "2026-10-12", "PCL-00002", "RDR-02", "2026-10-12 09:30:00", "FAILED", "NOT_HOME", "1", "0.00"],
    ],
    "odometer.csv": [
        ["rider_id", "odo_date", "start_km", "end_km"],
        ["RDR-01", "2026-10-12", "46611.90", "46674.56"],
        ["RDR-02", "2026-10-12", "21813.00", "21872.23"],
    ],
    "cod_deposits.csv": [
        ["deposit_id", "rider_id", "deposit_date", "deposit_ts", "amount"],
        ["DEP-0001", "RDR-01", "2026-10-12", "2026-10-12 18:06:00", "449.00"],
    ],
}


def write_csv(folder: Path, name: str, rows: list[list[str]]) -> None:
    with open(folder / name, "w", newline="") as fh:
        csv.writer(fh).writerows(rows)


@pytest.fixture
def data_dir(tmp_path):
    """A fresh valid folder for every test."""
    for name, rows in TINY.items():
        write_csv(tmp_path, name, rows)
    return tmp_path


@pytest.fixture
def edit(data_dir):
    """edit('parcels.csv', row=1, col='weight_kg', value='abc') changes one cell
    (row is the 1-based data row). edit_rows appends/removes via the tuple form."""
    def _edit(fname, row, col, value):
        path = data_dir / fname
        with open(path, newline="") as fh:
            rows = list(csv.reader(fh))
        rows[row][rows[0].index(col)] = value
        write_csv(data_dir, fname, rows)
    return _edit
