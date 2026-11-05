import ast
import math
import socket
from decimal import Decimal
from pathlib import Path

import pytest

import ps035.distance as distance_module
from ps035.distance import EARTH_RADIUS_KM, haversine_km

# One degree of arc on a sphere of radius R = 6371 km is 2*pi*R/360 km.
ONE_DEGREE_KM = 2 * math.pi * 6371 / 360  # 111.19492664...

# Coordinates copied from the official sample files (data/hub.csv, data/parcels.csv).
HUB = (12.903000, 77.624000)
PCL_251 = (12.962552, 77.591507)
PCL_501 = (12.898314, 77.651258)
PCL_502 = (12.892452, 77.685328)


def test_radius_is_exactly_6371():
    assert EARTH_RADIUS_KM == 6371


# ---- 1-3. same point and known pairs --------------------------------------

def test_same_point_is_zero():
    assert haversine_km(12.903, 77.624, 12.903, 77.624) == 0.0


def test_known_pair_one_degree_of_longitude_on_equator():
    # Calculation: 2*pi*6371/360 = 111.1949... km -> 111.19
    assert haversine_km(0, 0, 0, 1) == 111.19


def test_known_pair_pole_to_equator_is_a_quarter_circle():
    # Calculation: (pi/2)*6371 = 10007.5434... km -> 10007.54
    assert haversine_km(90, 0, 0, 0) == 10007.54


def test_known_pair_antipodes_are_half_the_circumference():
    # Calculation: pi*6371 = 20015.0868... km -> 20015.09
    assert haversine_km(0, 0, 0, 180) == 20015.09


def test_official_sample_hub_to_express_parcel_is_7_50_km():
    # README EC-02: PCL-00251 is "7.50 km from the hub" (R = 6371).
    assert haversine_km(*HUB, *PCL_251) == 7.50


def test_official_sample_ec03_legs():
    # README EC-03: hub -> 3.00 km -> +3.75 km -> 6.75 km on one ray.
    assert haversine_km(*HUB, *PCL_501) == 3.00
    assert haversine_km(*PCL_501, *PCL_502) == 3.75
    assert haversine_km(*HUB, *PCL_502) == 6.75


def test_real_world_london_to_paris_is_about_343_km():
    # Widely quoted great-circle distance is ~343-344 km; loose tolerance on purpose.
    assert haversine_km(51.5074, -0.1278, 48.8566, 2.3522) == pytest.approx(343.5, abs=1.0)


# ---- 4. symmetry ------------------------------------------------------------

@pytest.mark.parametrize("a,b", [(HUB, PCL_251), (PCL_501, PCL_502), ((51.5, -0.12), (-33.9, 151.2)), ((89.9, 10), (-89.9, -170))])
def test_distance_is_symmetric(a, b):
    assert haversine_km(*a, *b) == haversine_km(*b, *a)


# ---- 5. boundaries accepted -------------------------------------------------

@pytest.mark.parametrize("lat,lon", [(90, 0), (-90, 0), (0, 180), (0, -180), (90, 180), (-90, -180)])
def test_boundary_coordinates_accepted(lat, lon):
    assert haversine_km(lat, lon, 0, 0) >= 0


# ---- 6-7. out of range -------------------------------------------------------

@pytest.mark.parametrize("bad", [-90.0001, 90.0001, -91, 91, 1000])
def test_latitude_out_of_range_rejected(bad):
    with pytest.raises(ValueError, match="lat1"):
        haversine_km(bad, 0, 0, 0)
    with pytest.raises(ValueError, match="lat2"):
        haversine_km(0, 0, bad, 0)


@pytest.mark.parametrize("bad", [-180.0001, 180.0001, -181, 181, 360])
def test_longitude_out_of_range_rejected(bad):
    with pytest.raises(ValueError, match="lon1"):
        haversine_km(0, bad, 0, 0)
    with pytest.raises(ValueError, match="lon2"):
        haversine_km(0, 0, 0, bad)


# ---- 8-11. wrong types and non-finite numbers --------------------------------

@pytest.mark.parametrize("bad", ["12.9", "", "abc", [12.9], (12.9, 77.6), {"lat": 1}, b"1", 1 + 2j, object()])
def test_non_numeric_rejected_with_typeerror(bad):
    with pytest.raises(TypeError):
        haversine_km(bad, 0, 0, 0)
    with pytest.raises(TypeError):
        haversine_km(0, 0, 0, bad)


@pytest.mark.parametrize("bad", [True, False])
def test_booleans_rejected(bad):
    for position in range(4):
        args = [0, 0, 0, 0]
        args[position] = bad
        with pytest.raises(TypeError, match="bool"):
            haversine_km(*args)


def test_none_rejected():
    for position in range(4):
        args = [0, 0, 0, 0]
        args[position] = None
        with pytest.raises(TypeError, match="NoneType"):
            haversine_km(*args)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_nan_and_infinity_rejected_with_valueerror(bad):
    for position in range(4):
        args = [0, 0, 0, 0]
        args[position] = bad
        with pytest.raises(ValueError, match="finite"):
            haversine_km(*args)


def test_wrong_number_of_arguments_or_pair_style_rejected():
    with pytest.raises(TypeError):
        haversine_km((12.9, 77.6), (13.0, 77.7))  # tuples are not accepted
    with pytest.raises(TypeError):
        haversine_km(12.9, 77.6, 13.0)


def test_decimal_and_int_inputs_work_like_floats():
    # The loader produces Decimals, so they must give the same answer as floats.
    as_decimal = haversine_km(Decimal("12.903000"), Decimal("77.624000"), Decimal("12.962552"), Decimal("77.591507"))
    assert as_decimal == haversine_km(*HUB, *PCL_251) == 7.50
    assert haversine_km(0, 0, 0, 1) == haversine_km(0.0, 0.0, 0.0, 1.0)


# ---- 12. rounding ------------------------------------------------------------

def test_result_is_float_rounded_to_two_decimals():
    result = haversine_km(*HUB, *PCL_251)
    assert isinstance(result, float)
    assert result == round(result, 2)
    # raw value 111.19492664... must come back as exactly 111.19, not 111.1949...
    raw = haversine_km(0, 0, 0, 1)
    assert raw == 111.19 and raw != ONE_DEGREE_KM


def test_rounding_happens_once_at_the_end():
    # 2 degrees: unrounded 222.3898...; double-rounded paths would differ from this.
    assert haversine_km(0, 0, 0, 2) == round(2 * ONE_DEGREE_KM, 2) == 222.39


# ---- 13-14. antimeridian and poles -------------------------------------------

def test_antimeridian_crossing_takes_the_short_way():
    # 179.5E to 179.5W is 1 degree apart (NOT 359). 1 degree on the equator = 111.19 km.
    assert haversine_km(0, 179.5, 0, -179.5) == 111.19
    assert haversine_km(0, 180, 0, -180) == 0.0  # same meridian written two ways


def test_antimeridian_crossing_at_latitude():
    # At 60N a degree of longitude is cos(60 deg) = half as long: 1 degree of lon = 55.60 km.
    assert haversine_km(60, 179.5, 60, -179.5) == pytest.approx(55.6, abs=0.1)


def test_near_pole_points_on_opposite_meridians():
    # 89.9N, lon 0 and lon 180 are 0.2 degrees apart over the pole: 0.2*111.1949 = 22.24 km.
    assert haversine_km(89.9, 0, 89.9, 180) == 22.24


def test_exact_pole_ignores_longitude():
    assert haversine_km(90, 0, 90, 123.4) == 0.0
    assert haversine_km(-90, -50, -90, 77) == 0.0
    assert haversine_km(90, 0, -90, 0) == 20015.09  # pole to pole


# ---- 15. fully local ----------------------------------------------------------

def test_module_imports_only_local_standard_library():
    tree = ast.parse(Path(distance_module.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "math", "decimal"}


def test_works_with_networking_disabled(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    assert haversine_km(*HUB, *PCL_251) == 7.50


def test_is_deterministic():
    assert len({haversine_km(*HUB, *PCL_502) for _ in range(50)}) == 1
