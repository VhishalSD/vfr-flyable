#!/usr/bin/env python3
"""Reusable timezone and daylight calculations for airport metadata."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astral import Observer
from astral.sun import noon, sunrise, sunset


UTC = timezone.utc


def validated_zoneinfo(timezone_name: str) -> ZoneInfo:
    """Return a validated IANA ZoneInfo instance."""
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ValueError("timezone name must be a non-empty IANA identifier")
    try:
        return ZoneInfo(timezone_name.strip())
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"invalid IANA timezone: {timezone_name!r}") from error


def _as_utc(moment: datetime) -> datetime:
    if not isinstance(moment, datetime):
        raise TypeError("moment must be a datetime")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def utc_to_local(moment: datetime, timezone_name: str) -> datetime:
    """Convert a UTC datetime to an aware datetime in an IANA timezone."""
    return _as_utc(moment).astimezone(validated_zoneinfo(timezone_name))


def utc_offset(moment: datetime, timezone_name: str) -> timedelta:
    """Return the UTC offset applicable to ``moment`` in ``timezone_name``."""
    offset = utc_to_local(moment, timezone_name).utcoffset()
    if offset is None:
        raise ValueError(f"timezone {timezone_name!r} has no UTC offset")
    return offset


def utc_offset_minutes(moment: datetime, timezone_name: str) -> int:
    """Return the applicable UTC offset as whole minutes."""
    return int(utc_offset(moment, timezone_name).total_seconds() // 60)


def is_dst(moment: datetime, timezone_name: str) -> bool:
    """Return whether daylight-saving time is active at ``moment``."""
    dst = utc_to_local(moment, timezone_name).dst()
    return dst is not None and dst != timedelta(0)


def local_date(moment: datetime, timezone_name: str) -> date:
    """Return the local calendar date for a UTC moment and IANA timezone."""
    return utc_to_local(moment, timezone_name).date()


def _iso_utc(moment: datetime) -> str:
    return (
        moment.astimezone(UTC)
        .replace(microsecond=0)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _polar_state(error: Exception) -> Optional[str]:
    message = str(error).casefold()
    if "always above" in message or "never reaches" in message and "below" in message:
        return "polarDay"
    if "always below" in message or "never reaches" in message and "above" in message:
        return "polarNight"
    return None


def calculate_daylight(
    latitude: float,
    longitude: float,
    timezone_name: str,
    local_day: Union[date, str],
) -> Dict[str, Any]:
    """Calculate daylight events for an airport's local calendar date.

    Event timestamps are stored in UTC. Polar days have no discrete sunrise or
    sunset and use a 24-hour day length; polar nights use zero. Unexpected
    astronomical failures return ``unavailable`` without escaping this function.
    """
    zone = validated_zoneinfo(timezone_name)
    if isinstance(local_day, str):
        local_day = date.fromisoformat(local_day)
    if not isinstance(local_day, date):
        raise TypeError("local_day must be a date or ISO date string")
    if not -90 <= float(latitude) <= 90 or not -180 <= float(longitude) <= 180:
        raise ValueError("invalid latitude/longitude")

    observer = Observer(latitude=float(latitude), longitude=float(longitude))
    result: Dict[str, Any] = {
        "date": local_day.isoformat(),
        "sunriseUtc": None,
        "solarNoonUtc": None,
        "sunsetUtc": None,
        "dayLengthSeconds": None,
        "state": "unavailable",
    }

    try:
        result["solarNoonUtc"] = _iso_utc(noon(observer, date=local_day, tzinfo=zone))
    except (ArithmeticError, OverflowError, ValueError):
        pass

    try:
        rise = sunrise(observer, date=local_day, tzinfo=zone)
        set_ = sunset(observer, date=local_day, tzinfo=zone)
    except (ArithmeticError, OverflowError, ValueError) as error:
        state = _polar_state(error)
        if state:
            result["state"] = state
            result["dayLengthSeconds"] = 86400 if state == "polarDay" else 0
        return result

    rise_iso = _iso_utc(rise)
    set_iso = _iso_utc(set_)
    day_length = int((_parse_utc(set_iso) - _parse_utc(rise_iso)).total_seconds())
    if day_length < 0:
        return result

    result.update(
        {
            "sunriseUtc": rise_iso,
            "sunsetUtc": set_iso,
            "dayLengthSeconds": day_length,
            "state": "normal",
        }
    )
    return result
