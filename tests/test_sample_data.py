"""Loads the real official sample pack (copied into data/, never modified)."""
from pathlib import Path

import pytest

from ps035.loader import load_dataset

DATA = Path(__file__).resolve().parent.parent / "data"

pytestmark = pytest.mark.skipif(not (DATA / "parcels.csv").is_file(), reason="sample data not present")


def test_official_sample_loads_with_expected_row_counts():
    ds = load_dataset(DATA)
    assert len(ds.hubs) == 1
    assert len(ds.riders) == 8
    assert len(ds.shifts) == 48
    assert len(ds.parcels) == 1500
    assert len(ds.events) == 1530
    assert len(ds.odometer) == 48
    assert len(ds.cod_deposits) == 45
