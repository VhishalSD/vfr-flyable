#!/usr/bin/env python3
"""Shared deterministic geographic calculations for airport backend data."""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Tuple


EARTH_MEAN_RADIUS_KM = 6371.0088
DISTANCE_DECIMALS = 1


class GeoError(ValueError):
    """Raised for invalid geographic input."""


def _coordinate(value: Any, name: str, minimum: float, maximum: float) -> float:
    if value is None or isinstance(value, bool):
        raise GeoError(f"missing {name}")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise GeoError(f"invalid {name}: {value!r}") from error
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise GeoError(f"out-of-range {name}: {value!r}")
    return number


def normalize_longitude_delta(delta_degrees: float) -> float:
    """Wrap a longitude delta to [-180, 180)."""
    if not math.isfinite(delta_degrees):
        raise GeoError("longitude delta is not finite")
    return (delta_degrees + 180.0) % 360.0 - 180.0


def haversine_km(lat1: Any, lon1: Any, lat2: Any, lon2: Any) -> float:
    first_lat = _coordinate(lat1, "latitude", -90, 90)
    first_lon = _coordinate(lon1, "longitude", -180, 180)
    second_lat = _coordinate(lat2, "latitude", -90, 90)
    second_lon = _coordinate(lon2, "longitude", -180, 180)
    phi1 = math.radians(first_lat)
    phi2 = math.radians(second_lat)
    delta_phi = math.radians(second_lat - first_lat)
    delta_lambda = math.radians(normalize_longitude_delta(second_lon - first_lon))
    value = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    value = min(1.0, max(0.0, value))
    return EARTH_MEAN_RADIUS_KM * 2.0 * math.atan2(math.sqrt(value), math.sqrt(1.0 - value))


def initial_bearing_deg(lat1: Any, lon1: Any, lat2: Any, lon2: Any) -> float:
    first_lat = _coordinate(lat1, "latitude", -90, 90)
    first_lon = _coordinate(lon1, "longitude", -180, 180)
    second_lat = _coordinate(lat2, "latitude", -90, 90)
    second_lon = _coordinate(lon2, "longitude", -180, 180)
    if first_lat == second_lat and first_lon == second_lon:
        return 0.0
    phi1 = math.radians(first_lat)
    phi2 = math.radians(second_lat)
    delta_lambda = math.radians(normalize_longitude_delta(second_lon - first_lon))
    x = math.sin(delta_lambda) * math.cos(phi2)
    y = (
        math.cos(phi1) * math.sin(phi2)
        - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lambda)
    )
    return math.degrees(math.atan2(x, y)) % 360.0


def round_distance_km(distance: Any) -> float:
    value = float(distance)
    if not math.isfinite(value) or value < 0:
        raise GeoError("distance is invalid")
    factor = 10 ** DISTANCE_DECIMALS
    return math.floor(value * factor + 0.5) / factor


def round_bearing_deg(bearing: Any) -> int:
    value = float(bearing)
    if not math.isfinite(value):
        raise GeoError("bearing is invalid")
    return int(math.floor((value % 360.0) + 0.5)) % 360


def distance_and_bearing(
    lat1: Any, lon1: Any, lat2: Any, lon2: Any
) -> Tuple[float, int]:
    distance = haversine_km(lat1, lon1, lat2, lon2)
    bearing = initial_bearing_deg(lat1, lon1, lat2, lon2)
    return round_distance_km(distance), round_bearing_deg(bearing)


def decimal_degrees_to_dms(value: Any, coordinate: str) -> str:
    """Return compact whole-second DMS (DDMMSSN / DDDMMSSE).

    Whole seconds use decimal half-up rounding. Converting the full absolute
    angle to seconds before splitting naturally carries 60 seconds/minutes.
    Zero uses N for latitude and E for longitude.
    """
    kind = str(coordinate).strip().lower()
    if kind not in {"latitude", "longitude"}:
        raise GeoError("coordinate must be 'latitude' or 'longitude'")
    maximum = 90 if kind == "latitude" else 180
    number = _coordinate(value, kind, -maximum, maximum)
    total_seconds = int(
        (Decimal(str(abs(number))) * Decimal(3600)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    maximum_seconds = maximum * 3600
    total_seconds = min(total_seconds, maximum_seconds)
    degrees, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if kind == "latitude":
        hemisphere = "S" if number < 0 else "N"
        return f"{degrees:02d}{minutes:02d}{seconds:02d}{hemisphere}"
    hemisphere = "W" if number < 0 else "E"
    return f"{degrees:03d}{minutes:02d}{seconds:02d}{hemisphere}"
