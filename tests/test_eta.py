import ast
import socket
from datetime import time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import ps035.eta as eta_module
from ps035.distance import haversine_km
from ps035.eta import (
    StopTiming, calculate_stop_timing, format_hhmmss, is_return_allowed, is_within_window,
    service_duration, time_of_day, travel_duration,
)

MIN = timedelta(minutes=1)


def T(text):
    return time_of_day(text)


# ---- travel duration (4 min per km) ------------------------------------------

def test_travel_zero_km():
    assert travel_duration(0) == timedelta(0)


def test_travel_one_km_is_four_minutes():
    assert travel_duration(1) == 4 * MIN


def test_travel_fractional_seconds_are_kept():
    # 0.01 km * 4 min/km = 2.4 s exactly (2,400,000 microseconds).
    assert travel_duration(0.01) == timedelta(seconds=2, microseconds=400_000)
    assert travel_duration(Decimal("0.01")) == travel_duration(0.01)
    assert travel_duration(0.07) == timedelta(seconds=16, microseconds=800_000)


def test_travel_known_official_legs():
    assert travel_duration(7.50) == 30 * MIN      # README EC-02
    assert travel_duration(6.75) == 27 * MIN      # README EC-03 return leg


def test_travel_distance_is_not_rounded_again():
    # a 3-decimal distance keeps its extra precision: 0.001 km = 0.24 s
    assert travel_duration(Decimal("0.001")) == timedelta(microseconds=240_000)


def test_travel_float_noise_does_not_leak():
    assert travel_duration(0.1 + 0.2) == timedelta(seconds=72)  # 0.30000000000000004 km -> 72 s


def test_negative_distance_rejected():
    with pytest.raises(ValueError, match="negative"):
        travel_duration(-0.01)


@pytest.mark.parametrize("bad", [True, False, "1.0", "", None, [1], (1,), 1 + 0j])
def test_wrong_type_distance_rejected(bad):
    with pytest.raises(TypeError):
        travel_duration(bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), Decimal("NaN"), Decimal("Infinity")])
def test_nan_and_infinite_distance_rejected(bad):
    with pytest.raises(ValueError, match="finite"):
        travel_duration(bad)


# ---- service duration --------------------------------------------------------

def test_normal_service_is_three_minutes():
    assert service_duration(False) == 3 * MIN


def test_cod_service_is_eight_minutes_total():
    assert service_duration(True) == 8 * MIN


@pytest.mark.parametrize("bad", [1, 0, "true", "", None, [], 1.0])
def test_cod_flag_must_be_a_real_bool(bad):
    with pytest.raises(TypeError, match="bool"):
        service_duration(bad)


# ---- time parsing and formatting ---------------------------------------------

def test_hh_mm_and_hh_mm_ss_inputs():
    assert T("09:30") == T("09:30:00") == timedelta(hours=9, minutes=30)
    assert time_of_day(time(18, 5, 9)) == timedelta(hours=18, minutes=5, seconds=9)  # loader type
    assert time_of_day(timedelta(hours=1)) == timedelta(hours=1)


@pytest.mark.parametrize("bad", ["", "9:30", "09-30", "0930", "24:00:00", "12:60", "12:00:60", "ab:cd", "09:30:00.5", " 09:30", "09:30:00:00"])
def test_invalid_time_strings_rejected(bad):
    with pytest.raises(ValueError):
        time_of_day(bad)


@pytest.mark.parametrize("bad", [None, 930, 9.5, True])
def test_invalid_time_types_rejected(bad):
    with pytest.raises(TypeError):
        time_of_day(bad)


def test_time_timedelta_outside_one_day_rejected():
    with pytest.raises(ValueError):
        time_of_day(timedelta(hours=24))
    with pytest.raises(ValueError):
        time_of_day(timedelta(seconds=-1))


def test_format_is_zero_padded():
    assert format_hhmmss(timedelta(0)) == "00:00:00"
    assert format_hhmmss(timedelta(hours=9, minutes=5, seconds=7)) == "09:05:07"
    assert format_hhmmss(timedelta(hours=23, minutes=59, seconds=59)) == "23:59:59"


def test_format_rounds_half_up_to_whole_seconds():
    assert format_hhmmss(timedelta(hours=9, seconds=2, microseconds=400_000)) == "09:00:02"
    assert format_hhmmss(timedelta(hours=9, seconds=2, microseconds=500_000)) == "09:00:03"
    assert format_hhmmss(timedelta(hours=9, seconds=2, microseconds=600_000)) == "09:00:03"


# ---- midnight ----------------------------------------------------------------

def test_midnight_is_not_wrapped_comparisons_stay_correct():
    # 23:50 + 20 min is 24:10:00 (NOT 00:10:00), so it is after any window.
    s = calculate_stop_timing("23:50:00", 5, "09:00:00", "23:59:59", False)  # 5 km = 20 min
    assert s.arrival == timedelta(hours=24, minutes=10)
    assert s.within_window is False


def test_printing_a_time_past_midnight_is_rejected():
    with pytest.raises(ValueError, match="midnight"):
        format_hhmmss(timedelta(hours=24))
    with pytest.raises(ValueError, match="midnight"):
        format_hhmmss(timedelta(hours=23, minutes=59, seconds=59, microseconds=600_000))  # rounds to 24:00:00
    with pytest.raises(ValueError):
        format_hhmmss(timedelta(seconds=-1))


def test_inputs_must_be_within_one_day():
    with pytest.raises(ValueError):
        calculate_stop_timing("24:00:00", 1, "09:00:00", "12:00:00", False)


# ---- stop timing --------------------------------------------------------------

def test_arrival_time_is_departure_plus_travel():
    s = calculate_stop_timing("10:00:00", 2.5, "09:00:00", "12:00:00", False)  # 2.5 km = 10 min
    assert s.arrival == T("10:10:00")


def test_inside_window_does_not_wait():
    s = calculate_stop_timing("10:00:00", 2.5, "09:00:00", "12:00:00", False)
    assert s.wait == timedelta(0) and s.eta == s.arrival == T("10:10:00") and s.within_window is True


def test_early_arrival_waits_exactly_until_window_start():
    s = calculate_stop_timing("09:00:00", 2.5, "10:00:00", "12:00:00", False)  # arrives 09:10
    assert s.arrival == T("09:10:00")
    assert s.wait == 50 * MIN
    assert s.eta == T("10:00:00")           # waits until window_start
    assert s.within_window is True


def test_arrival_exactly_at_window_start_is_accepted():
    s = calculate_stop_timing("09:00:00", 2.5, "09:10:00", "12:00:00", False)
    assert s.eta == T("09:10:00") and s.wait == timedelta(0) and s.within_window is True


def test_arrival_exactly_at_window_end_is_accepted():
    # inclusive end: README says "ETA must be <= window_end"
    s = calculate_stop_timing("11:00:00", 15, "09:00:00", "12:00:00", False)  # 15 km = 60 min
    assert s.eta == T("12:00:00") and s.within_window is True


def test_one_second_after_window_end_is_infeasible_and_not_clamped():
    s = calculate_stop_timing("11:00:00", Decimal("15.01") - Decimal("0.0"), "09:00:00", "12:00:00", False)
    assert s.eta > T("12:00:00") and s.within_window is False   # 15.01 km = 60 min 2.4 s
    late = calculate_stop_timing("12:30:00", 1, "09:00:00", "12:00:00", False)
    assert late.eta == T("12:34:00")      # NOT clamped to 12:00:00
    assert late.within_window is False


def test_ec02_express_first_stop_exactly_0930():
    # README EC-02: 09:00 shift start, 7.50 km leg -> ETA exactly 09:30:00 (the 09:30 cut-off itself is a later module).
    s = calculate_stop_timing("09:00:00", 7.50, "09:00:00", "12:00:00", False)
    assert format_hhmmss(s.eta) == "09:30:00" and s.within_window


def test_next_departure_includes_service_after_waiting():
    s = calculate_stop_timing("09:00:00", 2.5, "10:00:00", "12:00:00", False)
    assert s.departure == T("10:03:00")        # wait to 10:00, then 3 min service
    cod = calculate_stop_timing("09:00:00", 2.5, "10:00:00", "12:00:00", True)
    assert cod.departure == T("10:08:00")      # COD: 8 min service
    assert cod.eta == s.eta                    # service time never moves the ETA


def test_result_is_immutable_dataclass():
    s = calculate_stop_timing("09:00:00", 1, "09:00:00", "12:00:00", False)
    assert isinstance(s, StopTiming)
    with pytest.raises(Exception):
        s.eta = timedelta(0)


def test_fractional_seconds_carry_through_the_route():
    # 0.01 km legs: 2.4 s each; the fractions add up exactly (no premature rounding).
    first = calculate_stop_timing("09:00:00", 0.01, "08:00:00", "12:00:00", False)
    second = calculate_stop_timing(first.departure, 0.01, "08:00:00", "12:00:00", False)
    assert second.arrival == T("09:00:00") + 2 * (travel_duration(0.01) ) + service_duration(False)
    assert second.arrival == timedelta(hours=9, seconds=4 + 180, microseconds=800_000)
    assert format_hhmmss(second.arrival) == "09:03:05"


def test_invalid_window_rejected():
    with pytest.raises(ValueError, match="window_end"):
        calculate_stop_timing("09:00:00", 1, "12:00:00", "09:00:00", False)


def test_leg_distance_comes_from_the_haversine_module():
    hub, p251 = (12.903000, 77.624000), (12.962552, 77.591507)
    leg = haversine_km(*hub, *p251)   # 7.50
    s = calculate_stop_timing("09:00:00", leg, "09:00:00", "12:00:00", False)
    assert format_hhmmss(s.eta) == "09:30:00"


# ---- window helper -------------------------------------------------------------

@pytest.mark.parametrize("when,ok", [("08:59:59", False), ("09:00:00", True), ("12:00:00", True), ("12:00:01", False), ("10:30", True)])
def test_is_within_window_is_inclusive(when, ok):
    assert is_within_window(when, "09:00:00", "12:00:00") is ok


def test_is_within_window_handles_time_past_midnight():
    assert is_within_window(timedelta(hours=25), "09:00:00", "12:00:00") is False


# ---- shift end boundary (BR-04 helper only) --------------------------------------

def test_ec03_route_returns_exactly_at_shift_end():
    # README EC-03: 09:00 start, legs 3.00 + 3.75 + 6.75 km (13.50 km = 54 min) + 2 stops * 3 min = 60 min.
    t = T("09:00:00")
    first = calculate_stop_timing(t, 3.00, "09:00:00", "10:00:00", False)
    second = calculate_stop_timing(first.departure, 3.75, "09:00:00", "10:00:00", False)
    back = second.departure + travel_duration(6.75)
    assert format_hhmmss(back) == "10:00:00"
    assert is_return_allowed(back, "10:00:00") is True


def test_shift_end_plus_sixty_minutes_boundary():
    end = "10:00:00"
    assert is_return_allowed(T("10:00:00"), end) is True
    assert is_return_allowed(T("10:00:01"), end) is True       # overtime allowed
    assert is_return_allowed(T("11:00:00"), end) is True       # exactly +60:00
    assert is_return_allowed(T("11:00:00") + timedelta(microseconds=1), end) is False
    assert is_return_allowed(T("11:00:01"), end) is False


def test_return_check_is_not_fooled_by_midnight():
    assert is_return_allowed(timedelta(hours=25), "18:00:00") is False
    assert is_return_allowed(timedelta(hours=24, minutes=30), "23:30:00") is True  # 23:30 + 60 = 24:30


# ---- local only --------------------------------------------------------------------

def test_module_imports_only_the_standard_library():
    tree = ast.parse(Path(eta_module.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "math", "re", "dataclasses", "datetime", "decimal"}


def test_works_with_networking_disabled(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    s = calculate_stop_timing("09:00:00", 7.50, "09:00:00", "12:00:00", True)
    assert format_hhmmss(s.eta) == "09:30:00"
