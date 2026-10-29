#!/usr/bin/env python3
"""Build seven local-calendar-day, per-station METAR history snapshots."""

from __future__ import annotations

import argparse
import email.utils
import json
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SOURCE_URL = "https://aviationweather.gov/api/data/metar"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIVE_PATH = PROJECT_ROOT / "data" / "metar-live.json"
METADATA_PATH = PROJECT_ROOT / "data" / "airport-metadata.json"
OUTPUT_DIR = PROJECT_ROOT / "data" / "metar-recent"

RETENTION_LOCAL_DAYS = 7
INITIAL_SEGMENT_HOURS = 168.0
MIN_SEGMENT_HOURS = 0.25
MAX_RESULTS = 400
REQUEST_INTERVAL_SECONDS = 0.65
REQUEST_TIMEOUT_SECONDS = 90
MAX_ATTEMPTS = 4
DEFAULT_WORKERS = 6
USER_AGENT = "vfr-flyable-school-project/1.0 (recent METAR history updater)"
ICAO_PATTERN = re.compile(r"^[A-Z]{4}$")
VALID_CATEGORIES = {"VFR", "MVFR", "IFR", "LIFR"}
VALID_METAR_TYPES = {"METAR", "SPECI", "SYNOP", "BUOY", "CMAN"}
CEILING_COVERS = {"BKN", "OVC", "OVX"}
VARIABLE_RANGE_PATTERN = re.compile(r"\b(?P<from>\d{3})V(?P<to>\d{3})\b")
RAW_VISIBILITY_PATTERN = re.compile(
    r"(?:\b(?:VRB|\d{3})\d{2,3}(?:G\d{2,3})?(?:KT|MPS)\b)"
    r"(?:\s+\d{3}V\d{3})?\s+"
    r"(?P<visibility>CAVOK|(?:M|P)?\d+(?:\s+\d+/\d+|/\d+)?SM|\d{4})\b"
)

LEGACY_OBSERVATION_FIELDS = frozenset({
    "obsTime",
    "reportTime",
    "receiptTime",
    "metarType",
    "flightCategory",
    "flightCategorySource",
    "weatherRaw",
    "temp",
    "visib",
    "visibilityQualifier",
    "sourceVisibility",
    "cloudLayers",
    "ceilingFt",
    "ceilingSource",
    "verticalVisibilityFt",
    "windDirection",
    "variableWind",
    "windSpeed",
    "windGust",
    "altimeterHpa",
    "rawOb",
})
OBSERVATION_FIELDS = LEGACY_OBSERVATION_FIELDS | frozenset({"dewPointC", "dewPointSource"})

# A temperature/dew-point group is a standalone METAR token. Restricting both
# halves to the ICAO representation prevents dates, visibility fractions and
# unrelated slash-delimited groups from being mistaken for dew point data.
TEMPERATURE_COMPONENT_PATTERN = r"(?:M?\d{2}|//)"
TEMPERATURE_DEW_POINT_TOKEN_PATTERN = re.compile(
    rf"^(?P<temperature>{TEMPERATURE_COMPONENT_PATTERN})/"
    rf"(?P<dewpoint>{TEMPERATURE_COMPONENT_PATTERN})$"
)


class UpdateError(RuntimeError):
    """Raised when no complete, safe recent-history update can be committed."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value).strip())
        if not match:
            return None
        number = float(match.group(0))
    return number if math.isfinite(number) else None


def compact_number(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return int(value) if value.is_integer() else value


def parse_metar_temperature_component(value: str) -> Optional[int]:
    """Parse one ICAO METAR temperature component (17, M02 or //)."""
    if value == "//":
        return None
    if not re.fullmatch(r"M?\d{2}", value):
        raise ValueError(f"Invalid METAR temperature component: {value!r}")
    magnitude = int(value[1:] if value.startswith("M") else value)
    return -magnitude if value.startswith("M") else magnitude


def parse_temperature_dew_point_token(token: str) -> Optional[Tuple[Optional[int], Optional[int]]]:
    """Return a validated METAR temperature/dew-point pair or ``None``."""
    match = TEMPERATURE_DEW_POINT_TOKEN_PATTERN.fullmatch(token.strip().upper())
    if match is None:
        return None
    return (
        parse_metar_temperature_component(match.group("temperature")),
        parse_metar_temperature_component(match.group("dewpoint")),
    )


def parse_dew_point_from_raw(raw_observation: Any) -> Optional[int]:
    """Extract dew point only from an unambiguous standalone METAR T/Td token."""
    if not isinstance(raw_observation, str):
        return None
    matches = []
    for token in raw_observation.strip().upper().split():
        parsed = parse_temperature_dew_point_token(token)
        if parsed is not None:
            matches.append(parsed)
    if len(matches) != 1:
        return None
    return matches[0][1]


def parse_timestamp(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def canonical_optional_timestamp(value: Any) -> Optional[str]:
    parsed = parse_timestamp(value)
    return iso_utc(parsed) if parsed is not None else None


def parse_altimeter(value: Any) -> Optional[float]:
    number = parse_number(value)
    if number is None:
        return None
    if 800 <= number <= 1100:
        return compact_number(number)
    if 20 <= number <= 35:
        return compact_number(number * 33.8638866667)
    if 80000 <= number <= 110000:
        return compact_number(number / 100)
    raise UpdateError(f"Unexpected AWC altim value: {value!r}")


def load_station_ids(path: Optional[Path] = None) -> List[str]:
    path = path or LIVE_PATH
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UpdateError(f"Cannot read current METAR station set: {error}") from error
    if not isinstance(document, list):
        raise UpdateError("metar-live.json must contain a JSON array")
    ids = {
        str(item.get("icaoId", "")).strip().upper()
        for item in document
        if isinstance(item, dict)
    }
    invalid = sorted(item for item in ids if item and not ICAO_PATTERN.fullmatch(item))
    if invalid:
        raise UpdateError(f"Invalid ICAO identifiers: {', '.join(invalid)}")
    valid = sorted(item for item in ids if ICAO_PATTERN.fullmatch(item))
    if not valid:
        raise UpdateError("No current METAR ICAO identifiers found")
    return valid


def load_timezones(path: Optional[Path] = None) -> Dict[str, str]:
    path = path or METADATA_PATH
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UpdateError(f"Cannot read airport metadata: {error}") from error
    airports = document.get("airports") if isinstance(document, dict) else None
    if not isinstance(airports, dict):
        raise UpdateError("airport-metadata.json has no airports object")
    result: Dict[str, str] = {}
    for icao, record in airports.items():
        zone_name = record.get("timezone") if isinstance(record, dict) else None
        if not isinstance(zone_name, str) or not zone_name.strip():
            continue
        try:
            ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
        result[str(icao).upper()] = zone_name
    return result


def local_calendar_window(
    zone_name: str, generated_at: Optional[datetime] = None
) -> Tuple[datetime, datetime, date, date]:
    zone = ZoneInfo(zone_name)
    end = generated_at or utc_now()
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    end = end.astimezone(timezone.utc)
    local_today = end.astimezone(zone).date()
    first_date = local_today - timedelta(days=RETENTION_LOCAL_DAYS - 1)
    start_local = datetime.combine(first_date, datetime_time.min, tzinfo=zone)
    return start_local.astimezone(timezone.utc), end, first_date, local_today


def chunk_window(
    start: datetime, end: datetime, maximum_hours: float = INITIAL_SEGMENT_HOURS
) -> Iterable[Tuple[datetime, datetime]]:
    cursor = start
    delta = timedelta(hours=maximum_hours)
    while cursor < end:
        segment_end = min(end, cursor + delta)
        yield cursor, segment_end
        cursor = segment_end


def request_url(icao: str, start: datetime, end: datetime) -> str:
    hours = (end - start).total_seconds() / 3600
    query = urllib.parse.urlencode(
        {
            "ids": icao,
            "hours": f"{hours:.6f}".rstrip("0").rstrip("."),
            "date": iso_utc(end),
            "format": "json",
        }
    )
    return f"{SOURCE_URL}?{query}"


class RateLimiter:
    def __init__(self, interval_seconds: float = REQUEST_INTERVAL_SECONDS):
        self.interval_seconds = interval_seconds
        self._lock = threading.Lock()
        self._next_request = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_request - now)
            self._next_request = max(now, self._next_request) + self.interval_seconds
        if delay:
            time.sleep(delay)


class FetchStats:
    def __init__(self) -> None:
        self.requests = 0
        self.retries = 0
        self.split_segments = 0
        self.statuses: Counter[int] = Counter()
        self.warnings: List[str] = []
        self.lock = threading.Lock()

    def record_status(self, status: int) -> None:
        with self.lock:
            self.requests += 1
            self.statuses[status] += 1

    def record_retry(self) -> None:
        with self.lock:
            self.retries += 1

    def record_split(self) -> None:
        with self.lock:
            self.split_segments += 1


def retry_after_seconds(headers: Any) -> Optional[float]:
    value = headers.get("Retry-After") if headers is not None else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed is None:
            return None
        return max(0.0, (parsed.astimezone(timezone.utc) - utc_now()).total_seconds())


def fetch_segment(
    icao: str,
    start: datetime,
    end: datetime,
    limiter: RateLimiter,
    stats: FetchStats,
) -> List[Dict[str, Any]]:
    url = request_url(icao, start, end)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        limiter.wait()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                status = response.status
                payload = response.read()
            stats.record_status(status)
            if status == 204:
                return []
            decoded = json.loads(payload.decode("utf-8"))
            if not isinstance(decoded, list) or any(
                not isinstance(item, dict) for item in decoded
            ):
                raise UpdateError(f"AWC returned corrupt JSON for {icao}")
            return decoded
        except urllib.error.HTTPError as error:
            stats.record_status(error.code)
            if error.code == 204:
                return []
            retryable = error.code in {429, 500, 502, 503, 504}
            if not retryable or attempt == MAX_ATTEMPTS:
                raise UpdateError(f"HTTP {error.code} for {icao}") from error
            delay = retry_after_seconds(error.headers)
        except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
            if attempt == MAX_ATTEMPTS:
                raise UpdateError(f"Request failed for {icao}: {error}") from error
            delay = None
        except UpdateError:
            raise
        stats.record_retry()
        time.sleep(delay if delay is not None else 2 ** (attempt - 1))
    raise UpdateError(f"Request failed for {icao}")


SegmentFetcher = Callable[[str, datetime, datetime], List[Dict[str, Any]]]


def fetch_window_segmented(
    icao: str,
    start: datetime,
    end: datetime,
    fetcher: SegmentFetcher,
    stats: Optional[FetchStats] = None,
) -> List[Dict[str, Any]]:
    records = fetcher(icao, start, end)
    duration_hours = (end - start).total_seconds() / 3600
    if len(records) < MAX_RESULTS:
        return records
    if duration_hours <= MIN_SEGMENT_HOURS:
        raise UpdateError(
            f"{icao} still reaches {MAX_RESULTS} records in a {duration_hours:.2f}h window"
        )
    if stats is not None:
        stats.record_split()
    midpoint = start + (end - start) / 2
    return fetch_window_segmented(icao, start, midpoint, fetcher, stats) + fetch_window_segmented(
        icao, midpoint, end, fetcher, stats
    )


def raw_visibility_token(raw: str) -> Optional[str]:
    match = RAW_VISIBILITY_PATTERN.search(raw)
    return match.group("visibility") if match else None


def visibility_values(value: Any, raw: str) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    number = compact_number(parse_number(value))
    source = raw_visibility_token(raw)
    if source is None and value is not None:
        source = str(value).strip() or None
    value_text = str(value).strip().upper() if value is not None else ""
    source_text = (source or "").upper()
    if "+" in value_text or value_text.startswith(">") or source_text in {"9999", "CAVOK"} or source_text.startswith("P"):
        qualifier = "greaterThan"
    elif value_text.startswith("<") or source_text.startswith("M"):
        qualifier = "lessThan"
    elif number is not None:
        qualifier = "exact"
    else:
        qualifier = None
    return number, qualifier, source


def normalize_clouds(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise UpdateError("AWC clouds is not an array")
    clouds: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise UpdateError("AWC cloud layer is not an object")
        cover = str(item.get("cover", "")).strip().upper() or None
        if not cover:
            continue
        base = parse_number(item.get("base"))
        top = parse_number(item.get("top"))
        clouds.append(
            {
                "cover": cover,
                "baseFtAgl": round(base) if base is not None else None,
                "topFtAgl": round(top) if top is not None else None,
            }
        )
    return clouds


def vertical_visibility(source: Dict[str, Any], clouds: Sequence[Dict[str, Any]]) -> Optional[int]:
    value = parse_number(source.get("vertVis"))
    if value is not None:
        return round(value)
    ovx = [
        layer["baseFtAgl"]
        for layer in clouds
        if layer.get("cover") == "OVX" and isinstance(layer.get("baseFtAgl"), int)
    ]
    return min(ovx) if ovx else None


def derive_ceiling(
    clouds: Sequence[Dict[str, Any]], vertical_visibility_ft: Optional[int]
) -> Tuple[Optional[int], Optional[str]]:
    candidates = [
        (int(layer["baseFtAgl"]), "cloudLayer")
        for layer in clouds
        if layer.get("cover") in CEILING_COVERS
        and isinstance(layer.get("baseFtAgl"), int)
    ]
    if vertical_visibility_ft is not None:
        candidates.append((vertical_visibility_ft, "verticalVisibility"))
    if not candidates:
        return None, None
    return min(candidates, key=lambda item: (item[0], item[1] != "verticalVisibility"))


def calculate_category(visibility: float, ceiling: int) -> str:
    if visibility < 1 or ceiling < 500:
        return "LIFR"
    if visibility < 3 or ceiling < 1000:
        return "IFR"
    if visibility <= 5 or ceiling <= 3000:
        return "MVFR"
    return "VFR"


def flight_category(
    value: Any, visibility: Optional[float], ceiling: Optional[int]
) -> Tuple[Optional[str], Optional[str]]:
    source = str(value).strip().upper() if value is not None else ""
    if source in VALID_CATEGORIES:
        return source, "AWC"
    if visibility is not None and ceiling is not None:
        return calculate_category(float(visibility), ceiling), "derived"
    return None, None


def normalize_wind_direction(value: Any) -> Optional[Any]:
    if isinstance(value, str) and value.strip().upper() == "VRB":
        return "VRB"
    number = parse_number(value)
    return int(round(number)) % 360 if number is not None else None


def variable_wind(value: Any, raw: str) -> Optional[Dict[str, Any]]:
    match = VARIABLE_RANGE_PATTERN.search(raw)
    if match:
        token = match.group(0)
        return {
            "type": "range",
            "from": int(match.group("from")),
            "to": int(match.group("to")),
            "raw": token,
        }
    if isinstance(value, str) and value.strip().upper() == "VRB":
        return {"type": "VRB", "from": None, "to": None, "raw": "VRB"}
    return None


def normalize_record(source: Dict[str, Any], expected_icao: str) -> Tuple[datetime, Dict[str, Any], Tuple[Any, ...]]:
    icao = str(source.get("icaoId", "")).strip().upper()
    observed_at = parse_timestamp(source.get("obsTime"))
    raw = source.get("rawOb")
    if icao != expected_icao or observed_at is None or not isinstance(raw, str) or not raw.strip():
        raise UpdateError(f"Invalid AWC METAR record for {expected_icao}")
    raw = raw.strip()
    clouds = normalize_clouds(source.get("clouds"))
    vertical = vertical_visibility(source, clouds)
    ceiling, ceiling_source = derive_ceiling(clouds, vertical)
    visibility, qualifier, source_visibility = visibility_values(source.get("visib"), raw)
    category, category_source = flight_category(source.get("fltCat"), visibility, ceiling)
    metar_type_source = str(source.get("metarType", "")).strip().upper()
    metar_type = metar_type_source if metar_type_source in VALID_METAR_TYPES else None
    dew_point = compact_number(parse_number(source.get("dewp")))
    record: Dict[str, Any] = {
        "obsTime": iso_utc(observed_at),
        "reportTime": canonical_optional_timestamp(source.get("reportTime")),
        "receiptTime": canonical_optional_timestamp(source.get("receiptTime")),
        "metarType": metar_type,
        "flightCategory": category,
        "flightCategorySource": category_source,
        "weatherRaw": str(source.get("wxString")).strip() if source.get("wxString") is not None else None,
        "temp": compact_number(parse_number(source.get("temp"))),
        "dewPointC": dew_point,
        "dewPointSource": "AWC" if dew_point is not None else None,
        "visib": visibility,
        "visibilityQualifier": qualifier,
        "sourceVisibility": source_visibility,
        "cloudLayers": clouds,
        "ceilingFt": ceiling,
        "ceilingSource": ceiling_source,
        "verticalVisibilityFt": vertical,
        "windDirection": normalize_wind_direction(source.get("wdir")),
        "variableWind": variable_wind(source.get("wdir"), raw),
        "windSpeed": compact_number(parse_number(source.get("wspd"))),
        "windGust": compact_number(parse_number(source.get("wgst"))),
        "altimeterHpa": parse_altimeter(source.get("altim")),
        "rawOb": raw,
    }
    completeness = sum(
        value is not None and value != [] for key, value in record.items() if key != "rawOb"
    )
    receipt = parse_timestamp(source.get("receiptTime"))
    rank = (completeness, receipt.timestamp() if receipt else float("-inf"), len(raw), raw)
    return observed_at, record, rank


def valid_number_or_null(value: Any) -> bool:
    return value is None or (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def validate_observation(record: Any) -> Tuple[bool, str]:
    if not isinstance(record, dict) or set(record) not in {
        LEGACY_OBSERVATION_FIELDS,
        OBSERVATION_FIELDS,
    }:
        return False, "observation fields do not match schema"
    if parse_timestamp(record.get("obsTime")) is None or not str(record.get("rawOb", "")).strip():
        return False, "obsTime/rawOb is invalid"
    for field_name in ("temp", "dewPointC", "visib", "windSpeed", "windGust", "altimeterHpa"):
        if not valid_number_or_null(record.get(field_name)):
            return False, f"{field_name} is not numeric/null"
    if record.get("dewPointSource") not in {"AWC", None}:
        return False, "dewPointSource is invalid"
    for field_name in ("ceilingFt", "verticalVisibilityFt"):
        value = record.get(field_name)
        if value is not None and (not isinstance(value, int) or value < 0):
            return False, f"{field_name} is invalid"
    if record.get("flightCategory") not in VALID_CATEGORIES | {None}:
        return False, "flightCategory is invalid"
    if record.get("flightCategorySource") not in {"AWC", "derived", None}:
        return False, "flightCategorySource is invalid"
    if record.get("ceilingSource") not in {"cloudLayer", "verticalVisibility", None}:
        return False, "ceilingSource is invalid"
    if not isinstance(record.get("cloudLayers"), list):
        return False, "cloudLayers is invalid"
    wind_direction = record.get("windDirection")
    if wind_direction != "VRB" and wind_direction is not None and not isinstance(wind_direction, int):
        return False, "windDirection is invalid"
    return True, "valid"


def validate_document(document: Any, expected_icao: Optional[str] = None) -> Tuple[bool, str]:
    if not isinstance(document, dict) or document.get("schemaVersion") != 1:
        return False, "invalid root/schemaVersion"
    icao = document.get("icao")
    if not isinstance(icao, str) or not ICAO_PATTERN.fullmatch(icao):
        return False, "invalid ICAO"
    if expected_icao is not None and icao != expected_icao:
        return False, "ICAO mismatch"
    try:
        zone = ZoneInfo(document.get("timezone"))
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return False, "invalid timezone"
    generated_at = parse_timestamp(document.get("generatedAt"))
    window_start = parse_timestamp(document.get("windowStart"))
    window_end = parse_timestamp(document.get("windowEnd"))
    if generated_at is None or window_start is None or window_end is None or window_start >= window_end:
        return False, "invalid timestamps/window"
    observations = document.get("observations")
    if not isinstance(observations, list) or document.get("recordCount") != len(observations):
        return False, "invalid observations/recordCount"
    previous: Optional[datetime] = None
    seen: set[str] = set()
    try:
        local_start = date.fromisoformat(document.get("localDateStart"))
        local_end = date.fromisoformat(document.get("localDateEnd"))
    except (TypeError, ValueError):
        return False, "invalid local dates"
    if (local_end - local_start).days != RETENTION_LOCAL_DAYS - 1:
        return False, "local date retention is not seven days"
    for record in observations:
        valid, detail = validate_observation(record)
        if not valid:
            return False, detail
        observed_at = parse_timestamp(record["obsTime"])
        assert observed_at is not None
        if previous is not None and observed_at <= previous:
            return False, "observations are not unique and chronological"
        if not (window_start <= observed_at <= window_end):
            return False, "observation outside UTC window"
        local_date = observed_at.astimezone(zone).date()
        if not (local_start <= local_date <= local_end):
            return False, "observation outside local calendar dates"
        if record["obsTime"] in seen:
            return False, "duplicate obsTime"
        seen.add(record["obsTime"])
        previous = observed_at
    return True, f"{icao}: {len(observations)} observations"


def load_existing_documents(output_dir: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    output_dir = output_dir or OUTPUT_DIR
    if not output_dir.exists():
        return {}
    if not output_dir.is_dir():
        raise UpdateError(f"{output_dir} is not a directory")
    documents: Dict[str, Dict[str, Any]] = {}
    for path in sorted(output_dir.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise UpdateError(f"Existing station file is corrupt; preserving dataset: {path}: {error}") from error
        valid, detail = validate_document(document, path.stem.upper())
        if not valid:
            raise UpdateError(f"Existing station file is invalid; preserving dataset: {path}: {detail}")
        documents[path.stem.upper()] = document
    return documents


def build_station_document(
    icao: str,
    zone_name: str,
    source_records: Sequence[Dict[str, Any]],
    generated_at: Optional[datetime] = None,
    existing_document: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    window_start, window_end, local_start, local_end = local_calendar_window(zone_name, generated_at)
    zone = ZoneInfo(zone_name)
    deduplicated: Dict[str, Tuple[datetime, Dict[str, Any], Tuple[Any, ...]]] = {}
    retained_existing = 0
    if existing_document is not None:
        valid, detail = validate_document(existing_document, icao)
        if not valid:
            raise UpdateError(f"Cannot merge invalid existing {icao}: {detail}")
        for record in existing_document["observations"]:
            observed_at = parse_timestamp(record["obsTime"])
            assert observed_at is not None
            if window_start <= observed_at <= window_end and local_start <= observed_at.astimezone(zone).date() <= local_end:
                rank = (sum(value is not None and value != [] for value in record.values()), float("-inf"), len(record["rawOb"]), record["rawOb"])
                deduplicated[record["obsTime"]] = (observed_at, dict(record), rank)
                retained_existing += 1

    source_used = 0
    for source in source_records:
        observed_at, record, rank = normalize_record(source, icao)
        if not (window_start <= observed_at <= window_end):
            continue
        if not (local_start <= observed_at.astimezone(zone).date() <= local_end):
            continue
        source_used += 1
        previous = deduplicated.get(record["obsTime"])
        if previous is None or rank > previous[2]:
            deduplicated[record["obsTime"]] = (observed_at, record, rank)

    observations = [item[1] for item in sorted(deduplicated.values(), key=lambda item: item[0])]
    generated = window_end
    document: Dict[str, Any] = {
        "schemaVersion": 1,
        "icao": icao,
        "generatedAt": iso_utc(generated),
        "timezone": zone_name,
        "windowStart": iso_utc(window_start),
        "windowEnd": iso_utc(window_end),
        "localDateStart": local_start.isoformat(),
        "localDateEnd": local_end.isoformat(),
        "source": {
            "provider": "NOAA Aviation Weather Center",
            "endpoint": SOURCE_URL,
            "windowQuery": "date=end anchor; hours searches backward",
        },
        "units": {
            "temp": "C",
            "dewPointC": "C",
            "visib": "SM",
            "cloudBase": "ft AGL",
            "ceiling": "ft AGL",
            "verticalVisibility": "ft",
            "wind": "kt",
            "altimeter": "hPa",
        },
        "recordCount": len(observations),
        "observations": observations,
    }
    valid, detail = validate_document(document, icao)
    if not valid:
        raise UpdateError(f"Generated {icao} failed validation: {detail}")
    return document, {"sourceUsed": source_used, "retainedExisting": retained_existing}


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_document(path: Path, document: Dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o644)


def commit_documents_transactionally(
    documents: Dict[str, Dict[str, Any]], output_dir: Optional[Path] = None
) -> None:
    output_dir = output_dir or OUTPUT_DIR
    parent = output_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".metar-recent-stage-", dir=parent))
    backup = parent / f".metar-recent-backup-{uuid.uuid4().hex}"
    old_moved = False
    new_moved = False
    try:
        for icao, document in sorted(documents.items()):
            valid, detail = validate_document(document, icao)
            if not valid:
                raise UpdateError(f"Refusing to stage invalid {icao}: {detail}")
            write_document(stage / f"{icao}.json", document)
        fsync_directory(stage)
        if output_dir.exists():
            os.replace(output_dir, backup)
            old_moved = True
        os.replace(stage, output_dir)
        new_moved = True
        fsync_directory(parent)
        if backup.exists():
            shutil.rmtree(backup)
    except Exception:
        if new_moved and output_dir.exists():
            shutil.rmtree(output_dir)
        if old_moved and backup.exists():
            os.replace(backup, output_dir)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if backup.exists() and output_dir.exists():
            shutil.rmtree(backup)


def fetch_station(
    icao: str,
    zone_name: str,
    generated_at: datetime,
    limiter: RateLimiter,
    stats: FetchStats,
    maximum_segment_hours: float,
) -> List[Dict[str, Any]]:
    start, end, _, _ = local_calendar_window(zone_name, generated_at)
    fetcher = lambda station, segment_start, segment_end: fetch_segment(
        station, segment_start, segment_end, limiter, stats
    )
    records: List[Dict[str, Any]] = []
    for segment_start, segment_end in chunk_window(start, end, maximum_segment_hours):
        records.extend(
            fetch_window_segmented(
                icao, segment_start, segment_end, fetcher, stats
            )
        )
    return records


def update(
    selected_ids: Optional[Sequence[str]] = None,
    workers: int = DEFAULT_WORKERS,
    request_interval: float = REQUEST_INTERVAL_SECONDS,
    maximum_segment_hours: float = INITIAL_SEGMENT_HOURS,
) -> Dict[str, Any]:
    all_ids = load_station_ids()
    requested = sorted(set(selected_ids or all_ids))
    unknown = sorted(set(requested) - set(all_ids))
    if unknown:
        raise UpdateError(f"Requested ICAO is outside current METAR set: {', '.join(unknown)}")
    timezones = load_timezones()
    missing_zones = [icao for icao in requested if icao not in timezones]
    if missing_zones:
        raise UpdateError(f"Missing valid timezone for: {', '.join(missing_zones)}")
    existing = load_existing_documents()
    generated_at = utc_now()
    limiter = RateLimiter(request_interval)
    fetch_stats = FetchStats()
    fetched: Dict[str, List[Dict[str, Any]]] = {}
    failures: Dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(
                fetch_station,
                icao,
                timezones[icao],
                generated_at,
                limiter,
                fetch_stats,
                maximum_segment_hours,
            ): icao
            for icao in requested
        }
        for future in as_completed(futures):
            icao = futures[future]
            try:
                fetched[icao] = future.result()
                print(f"Fetched {icao}: {len(fetched[icao])} source records", file=sys.stderr)
            except Exception as error:
                failures[icao] = str(error)

    # Network failures may reuse a still-valid prior station snapshot. A station
    # without such a snapshot makes the whole transaction fail before any write.
    unrecoverable = [icao for icao in failures if icao not in existing]
    if unrecoverable:
        raise UpdateError(
            "Unrecoverable station requests; no files changed: "
            + "; ".join(f"{icao}: {failures[icao]}" for icao in unrecoverable)
        )

    documents = dict(existing) if selected_ids is not None else {}
    build_stats: Dict[str, Dict[str, int]] = {}
    for icao in requested:
        if icao in failures:
            documents[icao] = existing[icao]
            continue
        document, station_stats = build_station_document(
            icao,
            timezones[icao],
            fetched[icao],
            generated_at=generated_at,
            existing_document=existing.get(icao),
        )
        documents[icao] = document
        build_stats[icao] = station_stats

    commit_documents_transactionally(documents)
    record_count = sum(document["recordCount"] for document in documents.values())
    type_counts: Counter[str] = Counter(
        observation.get("metarType") or "null"
        for document in documents.values()
        for observation in document["observations"]
    )
    return {
        "requestedStations": len(requested),
        "stationFiles": len(documents),
        "updatedStations": len(requested) - len(failures),
        "preservedFailedStations": len(failures),
        "records": record_count,
        "types": dict(type_counts),
        "requests": fetch_stats.requests,
        "retries": fetch_stats.retries,
        "splitSegments": fetch_stats.split_segments,
        "statuses": dict(fetch_stats.statuses),
        "failures": failures,
        "generatedAt": iso_utc(generated_at),
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ids",
        help="Comma-separated subset of current METAR ICAOs (default: all)",
    )
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--request-interval", type=float, default=REQUEST_INTERVAL_SECONDS)
    parser.add_argument("--segment-hours", type=float, default=INITIAL_SEGMENT_HOURS)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    selected = None
    if args.ids:
        selected = [item.strip().upper() for item in args.ids.split(",") if item.strip()]
    try:
        stats = update(
            selected_ids=selected,
            workers=args.workers,
            request_interval=args.request_interval,
            maximum_segment_hours=args.segment_hours,
        )
    except (OSError, UpdateError, ValueError) as error:
        print(f"Update failed: {error}", file=sys.stderr)
        return 1
    print(f"Source: {SOURCE_URL}")
    print(f"Generated at: {stats['generatedAt']}")
    print(f"Requested stations: {stats['requestedStations']}")
    print(f"Updated stations: {stats['updatedStations']}")
    print(f"Preserved after request failure: {stats['preservedFailedStations']}")
    print(f"Station files: {stats['stationFiles']}")
    print(f"Historical observations: {stats['records']}")
    print("METAR types: " + ", ".join(f"{key}={value}" for key, value in sorted(stats["types"].items())))
    print(f"HTTP requests: {stats['requests']}")
    print("HTTP statuses: " + ", ".join(f"{key}={value}" for key, value in sorted(stats["statuses"].items())))
    print(f"Retries: {stats['retries']}")
    print(f"Segments split at {MAX_RESULTS} records: {stats['splitSegments']}")
    print("Request failures: " + ("; ".join(f"{key}: {value}" for key, value in sorted(stats["failures"].items())) or "none"))
    print(f"Output: {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
