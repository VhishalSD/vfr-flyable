#!/usr/bin/env python3
"""Shared World Magnetic Model calculations for airport backend datasets.

Sign convention: signed declination is positive east and negative west.
Aviation conversion: magnetic heading = true heading - signed declination.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timezone
from functools import lru_cache
from typing import Any, Dict, Optional, Union

from pygeomag import GeoMag


MODEL_NAME = "WMM-2025"
MODEL_SOURCE = "NOAA/NCEI and British Geological Survey World Magnetic Model 2025"
MODEL_VALID_FROM = 2025.0
MODEL_VALID_TO = 2030.0
DISPLAY_DECIMALS = 1
SIGNED_DECLINATION_DECIMALS = 6


class MagneticModelError(ValueError):
    """Raised for an invalid or unavailable magnetic-model calculation."""


def _finite_number(value: Any, name: str) -> float:
    if value is None or isinstance(value, bool):
        raise MagneticModelError(f"missing {name}")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise MagneticModelError(f"invalid {name}: {value!r}") from error
    if not math.isfinite(number):
        raise MagneticModelError(f"non-finite {name}: {value!r}")
    return number


def decimal_year(value: Union[date, datetime]) -> float:
    """Convert a date/datetime to a deterministic UTC decimal year."""
    if isinstance(value, datetime):
        moment = value
        if moment.tzinfo is None:
            raise MagneticModelError("calculation datetime must be timezone-aware")
        moment = moment.astimezone(timezone.utc)
    elif isinstance(value, date):
        moment = datetime.combine(value, time.min, tzinfo=timezone.utc)
    else:
        raise MagneticModelError("calculation date must be date or datetime")
    start = datetime(moment.year, 1, 1, tzinfo=timezone.utc)
    end = datetime(moment.year + 1, 1, 1, tzinfo=timezone.utc)
    return moment.year + (moment - start).total_seconds() / (end - start).total_seconds()


@lru_cache(maxsize=1)
def magnetic_model() -> GeoMag:
    model = GeoMag()
    if model.model != MODEL_NAME or tuple(model.life_span) != (MODEL_VALID_FROM, MODEL_VALID_TO):
        raise MagneticModelError(
            f"unexpected magnetic model {model.model!r} with lifespan {model.life_span!r}"
        )
    return model


def calculate_signed_declination(
    latitude: Any,
    longitude: Any,
    calculation_date: Union[date, datetime],
    elevation_m: Any = 0,
) -> float:
    """Return WMM declination in degrees: east positive, west negative."""
    lat = _finite_number(latitude, "latitude")
    lon = _finite_number(longitude, "longitude")
    altitude_m = 0.0 if elevation_m is None else _finite_number(elevation_m, "elevation")
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise MagneticModelError("coordinates are outside their valid ranges")
    model_time = decimal_year(calculation_date)
    if not MODEL_VALID_FROM <= model_time < MODEL_VALID_TO:
        raise MagneticModelError(
            f"{MODEL_NAME} does not cover decimal year {model_time:.6f}"
        )
    try:
        result = magnetic_model().calculate(
            glat=lat,
            glon=lon,
            alt=altitude_m / 1000.0,
            time=model_time,
            allow_date_outside_lifespan=False,
            raise_in_warning_zone=True,
        )
    except Exception as error:
        raise MagneticModelError(f"{MODEL_NAME} calculation failed: {error}") from error
    declination = _finite_number(result.d, "declination")
    return round(declination, SIGNED_DECLINATION_DECIMALS)


def variation_fields(signed_declination: float, calculation_date: Union[date, datetime]) -> Dict[str, Any]:
    signed = _finite_number(signed_declination, "declination")
    calculation_day = calculation_date.date() if isinstance(calculation_date, datetime) else calculation_date
    if not isinstance(calculation_day, date):
        raise MagneticModelError("calculation date must be date or datetime")
    direction: Optional[str]
    if signed > 0:
        direction = "E"
    elif signed < 0:
        direction = "W"
    else:
        direction = None
    return {
        "signedDeclinationDeg": round(signed, SIGNED_DECLINATION_DECIMALS),
        "magneticVariationDeg": round(abs(signed), DISPLAY_DECIMALS),
        "magneticVariationDirection": direction,
        "magneticVariationYear": calculation_day.year,
        "magneticVariationDate": calculation_day.isoformat(),
        "magneticVariationSource": MODEL_NAME,
    }


def magnetic_heading(true_heading: Any, signed_declination: Any) -> Optional[int]:
    """Convert true to magnetic and round half-up to an integer in 0..359."""
    if true_heading is None or signed_declination is None:
        return None
    true_value = _finite_number(true_heading, "true heading")
    declination = _finite_number(signed_declination, "declination")
    normalized = (true_value - declination) % 360.0
    return int(math.floor(normalized + 0.5)) % 360
