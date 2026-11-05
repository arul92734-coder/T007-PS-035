"""Great-circle (Haversine) distance for ONE route leg.

Beginner notes
--------------
The Haversine formula gives the shortest distance over the Earth's surface
between two latitude/longitude points. BR-03 fixes the rules:

* Earth radius is exactly 6371 km.
* The answer is in kilometres, rounded to 2 decimal places.
* Only the FINAL answer is rounded; nothing in the middle is rounded.
* Each leg is calculated on its own. This module never adds legs together.
* Everything is local maths: no network, maps or geocoding.

Public function (the only one)::

    haversine_km(lat1, lon1, lat2, lon2) -> float

Four plain numbers are used (not tuples) because the loader's ``Hub`` and
``Parcel`` records already keep latitude and longitude as separate fields
(``hub.lat``, ``parcel.dest_lat`` ...), so values pass straight in.

Accepted number types: ``int``, ``float`` and ``decimal.Decimal`` (the loader
produces Decimals). Errors:

* ``TypeError``  - the value is not a real number type (str, None, tuple, ...),
  and also ``bool`` (``True`` is NOT accepted as 1).
* ``ValueError`` - it is a number but unusable: NaN, +/-infinity, latitude
  outside -90..90 or longitude outside -180..180. Values are never clamped.
"""
from __future__ import annotations

import math
from decimal import Decimal

EARTH_RADIUS_KM = 6371.0  # BR-03: exactly 6371 km

_NUMBER_TYPES = (int, float, Decimal)


def _check(value: object, name: str, limit: float) -> float:
    """Validate one coordinate and return it as a float."""
    if isinstance(value, bool) or not isinstance(value, _NUMBER_TYPES):
        raise TypeError(f"{name} must be a number (int, float or Decimal), got {type(value).__name__}: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    if not -limit <= number <= limit:
        raise ValueError(f"{name} must be between -{limit:g} and {limit:g} degrees, got {value!r}")
    return number


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the Haversine distance in km from (lat1, lon1) to (lat2, lon2).

    Latitudes are -90..90 and longitudes -180..180 (degrees, inclusive).
    The result is a float rounded to 2 decimal places.
    """
    la1 = math.radians(_check(lat1, "lat1", 90))
    lo1 = math.radians(_check(lon1, "lon1", 180))
    la2 = math.radians(_check(lat2, "lat2", 90))
    lo2 = math.radians(_check(lon2, "lon2", 180))

    d_lat = la2 - la1
    d_lon = lo2 - lo1  # antimeridian is fine: sin(d/2)**2 is the same for d and 360-d

    a = math.sin(d_lat / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(d_lon / 2) ** 2
    a = min(1.0, max(0.0, a))  # guard against tiny floating-point drift

    distance = 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))
    return round(distance, 2)  # the only rounding step
