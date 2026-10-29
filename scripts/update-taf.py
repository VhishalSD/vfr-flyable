#!/usr/bin/env python3
"""Refresh data/taf-live.json from the official AWC TAF Data API."""

from __future__ import annotations

import email.utils
import json
import math
import os
import random
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


API_URL = "https://aviationweather.gov/api/data/taf"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
AIRPORTS_PATH = PROJECT_ROOT / "data" / "airports-full.json"
OUTPUT_PATH = PROJECT_ROOT / "data" / "taf-live.json"
USER_AGENT = "vfr-flyable-local-taf-updater/1.0"
MAX_BATCH_SIZE = 250
REQUEST_TIMEOUT_SECONDS = 90
MAX_ATTEMPTS = 3
MIN_BATCH_INTERVAL_SECONDS = 60
MINIMUM_STATION_RETENTION = 0.70
ICAO_PATTERN = re.compile(r"^[A-Z]{4}$")
CHANGE_MARKER_PATTERN = re.compile(
    r"\bPROB(?P<combo_probability>\d{2})\s+TEMPO\s+\d{4}/\d{4}"
    r"|\bPROB(?P<probability>\d{2})\s+\d{4}/\d{4}"
    r"|\b(?P<period_change>TEMPO|BECMG)\s+\d{4}/\d{4}"
    r"|\b(?P<fm>FM)\d{6}"
)
TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}
GROUP_TYPES = ("BASELINE", "FM", "TEMPO", "BECMG", "PROB", "PROB_TEMPO")


class UpdateError(Exception):
    """Raised when a safe complete TAF update cannot be produced."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: Any, *, required: bool = False) -> Optional[str]:
    if value is None or value == "":
        if required:
            raise ValueError("required timestamp is missing")
        return None

    moment: datetime
    if isinstance(value, datetime):
        moment = value
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(timezone.utc)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise ValueError(f"invalid timestamp {value!r}")
        seconds = float(value)
        if abs(seconds) > 10_000_000_000:
            seconds /= 1000
        moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    elif isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            if required:
                raise ValueError("required timestamp is empty")
            return None
        if re.fullmatch(r"\d+(?:\.\d+)?", candidate):
            return iso_utc(float(candidate), required=required)
        candidate = candidate.replace(" ", "T", 1)
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(candidate)
        except ValueError as error:
            raise ValueError(f"invalid timestamp {value!r}") from error
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(timezone.utc)
    else:
        raise ValueError(f"invalid timestamp type {type(value).__name__}")
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        match = re.search(r"[-+]?\d+(?:\.\d+)?", value.strip())
        if not match:
            return None
        number = float(match.group(0))
    else:
        return None
    return number if math.isfinite(number) else None


def compact_number(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return int(value) if value.is_integer() else round(value, 4)


def parse_integer(value: Any) -> Optional[int]:
    number = parse_number(value)
    return round(number) if number is not None else None


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_station_ids(path: Optional[Path] = None) -> List[str]:
    source_path = path or AIRPORTS_PATH
    try:
        document = load_json(source_path)
    except (OSError, json.JSONDecodeError) as error:
        raise UpdateError(f"cannot read a valid {source_path}: {error}") from error
    if not isinstance(document, list):
        raise UpdateError(f"{source_path} must contain a JSON array")

    ids: List[str] = []
    for index, record in enumerate(document):
        if not isinstance(record, dict):
            raise UpdateError(f"airport record {index} in {source_path} is not an object")
        icao = str(record.get("icao", "")).strip().upper()
        if not ICAO_PATTERN.fullmatch(icao):
            raise UpdateError(f"airport record {index} in {source_path} has invalid ICAO {icao!r}")
        ids.append(icao)
    if not ids:
        raise UpdateError(f"{source_path} contains no ICAO stations")
    if len(ids) != len(set(ids)):
        duplicates = sorted(icao for icao, count in Counter(ids).items() if count > 1)
        raise UpdateError(f"{source_path} contains duplicate ICAOs: {', '.join(duplicates[:10])}")
    return sorted(ids)


def batches(values: Sequence[str], size: int = MAX_BATCH_SIZE) -> Iterable[List[str]]:
    if size < 1 or size > MAX_BATCH_SIZE:
        raise ValueError(f"batch size must be between 1 and {MAX_BATCH_SIZE}")
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


def retry_after_seconds(value: Optional[str], now: Optional[datetime] = None) -> float:
    if not value:
        return 0.0
    candidate = value.strip()
    try:
        return max(0.0, float(candidate))
    except ValueError:
        pass
    try:
        retry_at = email.utils.parsedate_to_datetime(candidate)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        current = now or utc_now()
        return max(0.0, (retry_at.astimezone(timezone.utc) - current).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return 0.0


def request_url(station_ids: Sequence[str]) -> str:
    query = urllib.parse.urlencode(
        {"ids": ",".join(station_ids), "format": "json", "time": "valid"},
        safe=",",
    )
    return f"{API_URL}?{query}"


def fetch_batch(
    station_ids: Sequence[str],
    batch_number: int,
    api_errors: List[str],
    status_log: Optional[List[int]] = None,
) -> Tuple[List[Dict[str, Any]], int, int]:
    url = request_url(station_ids)
    retries = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_SECONDS
            ) as response:
                status = int(getattr(response, "status", response.getcode()))
                payload = response.read()
            if status_log is not None:
                status_log.append(status)
            print(f"Batch {batch_number}: HTTP {status} ({len(station_ids)} ICAOs)")
            if status == 204:
                return [], status, retries
            if status != 200:
                raise UpdateError(f"batch {batch_number} returned HTTP {status}")
            try:
                document = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise UpdateError(
                    f"batch {batch_number} returned corrupt JSON: {error}"
                ) from error
            if not isinstance(document, list):
                raise UpdateError(
                    f"batch {batch_number} JSON root is not an array"
                )
            if any(not isinstance(item, dict) for item in document):
                raise UpdateError(f"batch {batch_number} contains a non-object TAF")
            return document, status, retries
        except urllib.error.HTTPError as error:
            status = error.code
            if status_log is not None:
                status_log.append(status)
            detail = f"batch {batch_number}, attempt {attempt}: HTTP {status}"
            if status not in TRANSIENT_HTTP_CODES or attempt == MAX_ATTEMPTS:
                api_errors.append(detail)
                raise UpdateError(detail) from error
            retries += 1
            delay = retry_after_seconds(error.headers.get("Retry-After"))
            if delay <= 0:
                delay = min(30.0, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
            api_errors.append(f"{detail}; retry in {delay:.1f}s")
            print(f"Batch {batch_number}: HTTP {status}; retry {attempt}/{MAX_ATTEMPTS - 1} in {delay:.1f}s")
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            detail = f"batch {batch_number}, attempt {attempt}: {error}"
            if attempt == MAX_ATTEMPTS:
                api_errors.append(detail)
                raise UpdateError(detail) from error
            retries += 1
            delay = min(30.0, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
            api_errors.append(f"{detail}; retry in {delay:.1f}s")
            print(f"Batch {batch_number}: network error; retry {attempt}/{MAX_ATTEMPTS - 1} in {delay:.1f}s")
            time.sleep(delay)
    raise UpdateError(f"batch {batch_number} exhausted retries")


def extract_change_markers(raw_taf: str) -> List[Dict[str, Any]]:
    markers: List[Dict[str, Any]] = []
    for match in CHANGE_MARKER_PATTERN.finditer(raw_taf.upper()):
        if match.group("combo_probability"):
            markers.append(
                {"type": "PROB_TEMPO", "probability": int(match.group("combo_probability"))}
            )
        elif match.group("probability"):
            markers.append(
                {"type": "PROB", "probability": int(match.group("probability"))}
            )
        elif match.group("period_change"):
            markers.append({"type": match.group("period_change"), "probability": None})
        else:
            markers.append({"type": "FM", "probability": None})
    return markers


def visibility_fields(value: Any) -> Tuple[Optional[float], str, Any]:
    if value is None:
        return None, "unknown", None
    source = value
    text = str(value).strip()
    number = parse_number(value)
    qualifier = "exact"
    if text.endswith("+"):
        qualifier = "greaterThan"
    elif text.startswith(">"):
        qualifier = "greaterThan"
    elif text.startswith("<"):
        qualifier = "lessThan"
    return compact_number(number), qualifier if number is not None else "unknown", source


def normalize_wind_direction(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().upper() == "VRB":
        return "VRB"
    return parse_integer(value)


def normalize_clouds(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("clouds is not an array")
    clouds: List[Dict[str, Any]] = []
    for cloud in value:
        if not isinstance(cloud, dict):
            raise ValueError("cloud layer is not an object")
        cover = str(cloud.get("cover", "")).strip().upper() or None
        clouds.append(
            {
                "cover": cover,
                "baseFtAgl": parse_integer(cloud.get("base")),
                "topFtAgl": parse_integer(cloud.get("top")),
            }
        )
    return clouds


def derive_ceiling(
    clouds: Sequence[Dict[str, Any]], vertical_visibility: Optional[int]
) -> Optional[int]:
    candidates = [
        int(cloud["baseFtAgl"])
        for cloud in clouds
        if cloud.get("cover") in {"BKN", "OVC", "OVX"}
        and isinstance(cloud.get("baseFtAgl"), int)
    ]
    if vertical_visibility is not None:
        candidates.append(vertical_visibility)
    return min(candidates) if candidates else None


def calculate_category(
    visibility: Optional[float], ceiling: Optional[int]
) -> str:
    if visibility is None and ceiling is None:
        return "UNKNOWN"
    if (visibility is not None and visibility < 1) or (
        ceiling is not None and ceiling < 500
    ):
        return "LIFR"
    if (visibility is not None and visibility < 3) or (
        ceiling is not None and ceiling < 1000
    ):
        return "IFR"
    if (visibility is not None and visibility <= 5) or (
        ceiling is not None and ceiling <= 3000
    ):
        return "MVFR"
    return "VFR"


def normalize_icing_turbulence(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("icgTurb is not an array")
    normalized: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("icing/turbulence entry is not an object")
        normalized.append(
            {
                "type": item.get("var"),
                "intensity": parse_integer(item.get("intensity")),
                "minAltitudeFt": parse_integer(item.get("minAlt")),
                "maxAltitudeFt": parse_integer(item.get("maxAlt")),
            }
        )
    return normalized


def normalize_temperatures(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("temp is not an array")
    normalized: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("temperature entry is not an object")
        normalized.append(
            {
                "validTime": iso_utc(item.get("validTime"), required=True),
                "temperatureC": compact_number(parse_number(item.get("sfcTemp"))),
                "kind": item.get("maxOrMin"),
            }
        )
    return normalized


def marker_for_group(
    sequence: int, markers: Sequence[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    marker_index = sequence - 1
    if marker_index < 0 or marker_index >= len(markers):
        return None
    return markers[marker_index]


def classify_group(
    sequence: int,
    source_change: Optional[str],
    source_probability: Optional[int],
    marker: Optional[Dict[str, Any]],
) -> Tuple[str, Optional[str], Optional[int]]:
    change = source_change.upper() if source_change else None
    probability = source_probability

    if marker and probability is None:
        probability = marker.get("probability")
    if marker and marker.get("type") == "PROB_TEMPO" and change in {None, "PROB", "TEMPO"}:
        return "PROB_TEMPO", "TEMPO", probability
    if change == "TEMPO" and probability is not None:
        return "PROB_TEMPO", "TEMPO", probability
    if change in {"FM", "TEMPO", "BECMG", "PROB"}:
        return change, change, probability
    if marker and marker.get("type") in {"FM", "TEMPO", "BECMG", "PROB"}:
        marker_type = str(marker["type"])
        return marker_type, marker_type, probability
    if sequence == 0:
        return "BASELINE", None, probability
    return "BASELINE", None, probability


def normalize_group(
    group: Dict[str, Any],
    sequence: int,
    markers: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    source_change_value = group.get("fcstChange")
    source_change = (
        str(source_change_value).strip().upper() if source_change_value else None
    )
    source_probability = parse_integer(group.get("probability"))
    group_type, change_indicator, probability = classify_group(
        sequence,
        source_change,
        source_probability,
        marker_for_group(sequence, markers),
    )
    visibility, visibility_qualifier, source_visibility = visibility_fields(
        group.get("visib")
    )
    vertical_visibility = parse_integer(group.get("vertVis"))
    clouds = normalize_clouds(group.get("clouds"))
    ceiling = derive_ceiling(clouds, vertical_visibility)
    weather_raw = (
        str(group.get("wxString")).strip() if group.get("wxString") is not None else None
    )
    if weather_raw == "":
        weather_raw = None

    valid_from = iso_utc(group.get("timeFrom"), required=True)
    valid_to = iso_utc(group.get("timeTo"), required=True)
    if valid_from >= valid_to:
        raise ValueError("forecast group validFrom must precede validTo")

    return {
        "sequence": sequence,
        "type": group_type,
        "changeIndicator": change_indicator,
        "probability": probability,
        "validFrom": valid_from,
        "validTo": valid_to,
        "becomingCompleteAt": iso_utc(group.get("timeBec")),
        "windDirection": normalize_wind_direction(group.get("wdir")),
        "windSpeed": compact_number(parse_number(group.get("wspd"))),
        "windGust": compact_number(parse_number(group.get("wgst"))),
        "visibilitySm": visibility,
        "visibilityQualifier": visibility_qualifier,
        "sourceVisibility": source_visibility,
        "weatherRaw": weather_raw,
        "weatherCodes": weather_raw.split() if weather_raw else [],
        "cloudLayers": clouds,
        "verticalVisibilityFt": vertical_visibility,
        "ceilingFt": ceiling,
        "ceilingSource": "derived",
        "flightCategory": calculate_category(visibility, ceiling),
        "flightCategorySource": "derived",
        "altimeterHpa": compact_number(parse_number(group.get("altim"))),
        "windShear": {
            "heightFt": parse_integer(group.get("wshearHgt")),
            "direction": normalize_wind_direction(group.get("wshearDir")),
            "speedKt": compact_number(parse_number(group.get("wshearSpd"))),
        },
        "icingTurbulence": normalize_icing_turbulence(group.get("icgTurb")),
        "temperatureExtremes": normalize_temperatures(group.get("temp")),
        "notDecoded": group.get("notDecoded"),
        "sourceFields": {
            "fcstChange": source_change,
            "probability": source_probability,
        },
    }


def record_rank(record: Dict[str, Any]) -> Tuple[int, str, str, str]:
    most_recent = 1 if record.get("mostRecent") in {1, True, "1"} else 0
    timestamps: List[str] = []
    for field in ("issueTime", "dbPopTime", "bulletinTime"):
        try:
            timestamps.append(iso_utc(record.get(field)) or "")
        except ValueError:
            timestamps.append("")
    return most_recent, timestamps[0], timestamps[1], timestamps[2]


def select_latest(records: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    by_icao: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        icao = str(record.get("icaoId", "")).strip().upper()
        if not ICAO_PATTERN.fullmatch(icao):
            raise UpdateError(f"AWC record has invalid ICAO {icao!r}")
        by_icao.setdefault(icao, []).append(record)
    return {
        icao: max(candidates, key=record_rank)
        for icao, candidates in sorted(by_icao.items())
    }


def normalize_station(record: Dict[str, Any]) -> Dict[str, Any]:
    icao = str(record.get("icaoId", "")).strip().upper()
    raw_taf = str(record.get("rawTAF", "")).strip()
    if not ICAO_PATTERN.fullmatch(icao):
        raise ValueError(f"invalid ICAO {icao!r}")
    if not raw_taf:
        raise ValueError(f"{icao} has no rawTAF")
    forecasts = record.get("fcsts")
    if not isinstance(forecasts, list) or not forecasts:
        raise ValueError(f"{icao} has no forecast groups")

    valid_from = iso_utc(record.get("validTimeFrom"), required=True)
    valid_to = iso_utc(record.get("validTimeTo"), required=True)
    if valid_from >= valid_to:
        raise ValueError(f"{icao} validity start must precede end")
    markers = extract_change_markers(raw_taf)
    groups = [
        normalize_group(group, sequence, markers)
        for sequence, group in enumerate(forecasts)
    ]

    return {
        "icaoId": icao,
        "rawTAF": raw_taf,
        "issueTime": iso_utc(record.get("issueTime"), required=True),
        "bulletinTime": iso_utc(record.get("bulletinTime")),
        "receivedTime": iso_utc(record.get("dbPopTime")),
        "validFrom": valid_from,
        "validTo": valid_to,
        "mostRecent": record.get("mostRecent") in {1, True, "1"},
        "remarks": record.get("remarks"),
        "prior": parse_integer(record.get("prior")),
        "location": {
            "name": record.get("name"),
            "latitude": compact_number(parse_number(record.get("lat"))),
            "longitude": compact_number(parse_number(record.get("lon"))),
            "elevationM": parse_integer(record.get("elev")),
        },
        "forecastGroups": groups,
    }


def validate_document(
    document: Any,
    *,
    allow_empty: bool = False,
    allowed_ids: Optional[set[str]] = None,
) -> Tuple[bool, str]:
    if not isinstance(document, dict):
        return False, "root is not an object"
    if document.get("schemaVersion") != 1:
        return False, "schemaVersion is not 1"
    try:
        iso_utc(document.get("generatedAt"), required=True)
    except ValueError as error:
        return False, f"invalid generatedAt: {error}"
    airports = document.get("airports")
    if not isinstance(airports, dict):
        return False, "airports is not an object"
    if not airports and not allow_empty:
        return False, "airports is empty"
    requested_count = document.get("requestedStationCount")
    if not isinstance(requested_count, int) or isinstance(requested_count, bool):
        return False, "requestedStationCount is invalid"
    if requested_count < len(airports):
        return False, "requestedStationCount is smaller than airports"
    if document.get("availableStationCount") != len(airports):
        return False, "availableStationCount does not match airports"
    if document.get("recordCount") != len(airports):
        return False, "recordCount does not match airports"
    if allowed_ids is not None and not set(airports).issubset(allowed_ids):
        unexpected = sorted(set(airports) - allowed_ids)
        return False, f"airports contains unrequested stations: {', '.join(unexpected[:5])}"
    source = document.get("source")
    if (
        not isinstance(source, dict)
        or source.get("provider") != "NOAA Aviation Weather Center"
        or source.get("endpoint") != API_URL
    ):
        return False, "source metadata is invalid"

    required_group_fields = {
        "sequence", "type", "changeIndicator", "probability", "validFrom",
        "validTo", "becomingCompleteAt", "windDirection", "windSpeed",
        "windGust", "visibilitySm", "visibilityQualifier", "sourceVisibility",
        "weatherRaw", "weatherCodes", "cloudLayers", "verticalVisibilityFt",
        "ceilingFt", "ceilingSource", "flightCategory",
        "flightCategorySource", "altimeterHpa", "windShear",
        "icingTurbulence", "temperatureExtremes", "notDecoded", "sourceFields",
    }
    for icao, station in airports.items():
        if not ICAO_PATTERN.fullmatch(icao) or not isinstance(station, dict):
            return False, f"invalid station {icao!r}"
        if station.get("icaoId") != icao or not str(station.get("rawTAF", "")).strip():
            return False, f"invalid identity/raw TAF for {icao}"
        try:
            issue = iso_utc(station.get("issueTime"), required=True)
            valid_from = iso_utc(station.get("validFrom"), required=True)
            valid_to = iso_utc(station.get("validTo"), required=True)
        except ValueError as error:
            return False, f"invalid station time for {icao}: {error}"
        if not issue or not valid_from or not valid_to or valid_from >= valid_to:
            return False, f"invalid validity for {icao}"
        groups = station.get("forecastGroups")
        if not isinstance(groups, list) or not groups:
            return False, f"no forecast groups for {icao}"
        for sequence, group in enumerate(groups):
            if not isinstance(group, dict) or not required_group_fields.issubset(group):
                return False, f"invalid forecast group {sequence} for {icao}"
            if group.get("sequence") != sequence or group.get("type") not in GROUP_TYPES:
                return False, f"invalid group sequence/type for {icao}"
            try:
                group_from = iso_utc(group.get("validFrom"), required=True)
                group_to = iso_utc(group.get("validTo"), required=True)
            except ValueError as error:
                return False, f"invalid group time for {icao}: {error}"
            if not group_from or not group_to or group_from >= group_to:
                return False, f"invalid group validity for {icao}"
    return True, f"{len(airports)} stations"


def load_previous_valid(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    target = path or OUTPUT_PATH
    try:
        document = load_json(target)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(document, dict) and "recordCount" not in document:
        airports = document.get("airports")
        if isinstance(airports, dict):
            document = {**document, "recordCount": len(airports)}
    valid, _ = validate_document(document)
    return document if valid else None


def ensure_safe_station_count(
    new_document: Dict[str, Any], previous_document: Optional[Dict[str, Any]]
) -> None:
    new_count = len(new_document["airports"])
    if new_count == 0:
        raise UpdateError("empty TAF dataset refused")
    if not previous_document:
        return
    previous_count = len(previous_document["airports"])
    minimum = max(1, math.floor(previous_count * MINIMUM_STATION_RETENTION))
    if previous_count >= 10 and new_count < minimum:
        raise UpdateError(
            f"station count dropped unexpectedly from {previous_count} to {new_count} "
            f"(minimum accepted {minimum})"
        )


def atomic_write(document: Dict[str, Any], path: Optional[Path] = None) -> None:
    target = path or OUTPUT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=".taf-live-",
            suffix=".json",
            delete=False,
        ) as handle:
            temp_name = handle.name
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        target.chmod(0o644)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def build_document(
    requested_ids: Sequence[str], records: Sequence[Dict[str, Any]], generated_at: str
) -> Tuple[Dict[str, Any], List[str]]:
    requested = set(requested_ids)
    selected = select_latest(records)
    unexpected = sorted(set(selected) - requested)
    if unexpected:
        raise UpdateError(f"AWC returned unrequested stations: {', '.join(unexpected[:5])}")

    airports: Dict[str, Any] = {}
    validation_errors: List[str] = []
    for icao, record in selected.items():
        try:
            airports[icao] = normalize_station(record)
        except (TypeError, ValueError) as error:
            validation_errors.append(f"{icao}: {error}")
    if validation_errors:
        raise UpdateError(
            "normalization failed: " + "; ".join(validation_errors[:10])
        )

    document = {
        "schemaVersion": 1,
        "generatedAt": generated_at,
        "source": {
            "provider": "NOAA Aviation Weather Center",
            "endpoint": API_URL,
            "format": "json",
            "selection": "currently valid, most recent",
            "stationSet": "data/airports-full.json",
        },
        "units": {
            "windSpeed": "kt",
            "visibility": "SM",
            "cloudBase": "ft AGL",
            "cloudTop": "ft AGL",
            "verticalVisibility": "ft",
            "windShearHeight": "ft",
            "altimeter": "hPa",
            "temperature": "C",
            "elevation": "m",
        },
        "requestedStationCount": len(requested_ids),
        "availableStationCount": len(airports),
        "recordCount": len(airports),
        "airports": airports,
    }
    return document, validation_errors


def group_counts(document: Dict[str, Any]) -> Counter[str]:
    return Counter(
        group["type"]
        for station in document["airports"].values()
        for group in station["forecastGroups"]
    )


def main() -> int:
    api_errors: List[str] = []
    validation_errors: List[str] = []
    http_statuses: List[int] = []
    requested_ids: List[str] = []
    batch_list: List[List[str]] = []
    try:
        requested_ids = load_station_ids()
        batch_list = list(batches(requested_ids))
        print(f"Source: {API_URL}")
        print(f"requestedStationCount: {len(requested_ids)}")
        print(f"Batches: {len(batch_list)} (maximum {MAX_BATCH_SIZE} ICAOs each)")

        all_records: List[Dict[str, Any]] = []
        for index, station_batch in enumerate(batch_list, start=1):
            if index > 1:
                time.sleep(MIN_BATCH_INTERVAL_SECONDS)
            records, _, _ = fetch_batch(
                station_batch, index, api_errors, http_statuses
            )
            all_records.extend(records)

        generated_at = iso_utc(utc_now(), required=True)
        document, validation_errors = build_document(
            requested_ids, all_records, generated_at
        )
        valid, detail = validate_document(document, allowed_ids=set(requested_ids))
        if not valid:
            validation_errors.append(detail)
            raise UpdateError(f"document validation failed: {detail}")
        ensure_safe_station_count(document, load_previous_valid())
        atomic_write(document)

        counts = group_counts(document)
        total_groups = sum(counts.values())
        print(f"HTTP statuses: {', '.join(map(str, http_statuses)) or 'none'}")
        print(f"availableStationCount: {document['availableStationCount']}")
        print(
            "Stations without current TAF: "
            f"{len(requested_ids) - document['availableStationCount']}"
        )
        print(f"Forecast groups: {total_groups}")
        print(
            "Group types: "
            + ", ".join(f"{kind}={counts.get(kind, 0)}" for kind in GROUP_TYPES)
        )
        print(f"Validation errors: {len(validation_errors)}")
        retry_events = sum("retry in" in error for error in api_errors)
        print(f"API/retry errors: {len(api_errors)}; retries: {retry_events}")
        for error in api_errors:
            print(f"  - {error}")
        print(f"Output: {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size} bytes)")
        print(f"generatedAt: {document['generatedAt']}")
        return 0
    except (UpdateError, OSError, ValueError) as error:
        print(f"TAF update failed: {error}", file=sys.stderr)
        print(f"requestedStationCount: {len(requested_ids)}", file=sys.stderr)
        print(f"Batches: {len(batch_list)}", file=sys.stderr)
        print(f"HTTP statuses: {', '.join(map(str, http_statuses)) or 'none'}", file=sys.stderr)
        print(f"Validation errors: {len(validation_errors)}", file=sys.stderr)
        retry_events = sum("retry in" in api_error for api_error in api_errors)
        print(f"API/retry errors: {len(api_errors)}; retries: {retry_events}", file=sys.stderr)
        for api_error in api_errors:
            print(f"  - {api_error}", file=sys.stderr)
        print("Existing taf-live.json preserved.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
