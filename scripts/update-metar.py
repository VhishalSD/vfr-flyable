#!/usr/bin/env python3
"""Refresh data/metar-live.json from the official AWC METAR cache."""

from __future__ import annotations

import gzip
import email.utils
import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SOURCE_URL = "https://aviationweather.gov/data/cache/metars.cache.xml.gz"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
AIRPORTS_PATH = PROJECT_ROOT / "data" / "airports-full.json"
OUTPUT_PATH = PROJECT_ROOT / "data" / "metar-live.json"
USER_AGENT = "vfr-flyable-metar-updater/2.0 (+local-development)"
REQUEST_TIMEOUT_SECONDS = 90
MAX_ATTEMPTS = 3
TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}
MINIMUM_STATION_RETENTION = 0.8

ALLOWED_COUNTRIES = {
    "Netherlands": "EH",
    "Belgium": "EB",
    "Germany": "ED",
    "France": "LF",
    "United Kingdom": "EG",
}
VALID_CATEGORIES = {"VFR", "MVFR", "IFR", "LIFR"}
ICAO_PATTERN = re.compile(r"^[A-Z]{4}$")
RAW_METAR_PATTERN = re.compile(r"^(?:METAR|SPECI)\s+([A-Z]{4})(?:\s|$)", re.IGNORECASE)
NUMERIC_FIELDS = (
    "visib",
    "wdir",
    "wspd",
    "wgst",
    "altim",
    "ceiling",
    "temp",
    "dewp",
    "lat",
    "lon",
)


class UpdateError(RuntimeError):
    """Raised when a source snapshot cannot safely be published."""


def parse_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value.strip())
    if not match:
        return None
    number = float(match.group(0))
    return number if math.isfinite(number) else None


def compact_number(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return int(value) if value.is_integer() else value


def text(node: ET.Element, name: str) -> Optional[str]:
    child = node.find(name)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def load_allowed_airports() -> Dict[str, Dict[str, Any]]:
    with AIRPORTS_PATH.open(encoding="utf-8") as handle:
        airports = json.load(handle)

    if not isinstance(airports, list):
        raise ValueError(f"{AIRPORTS_PATH} must contain a JSON array")

    allowed: Dict[str, Dict[str, Any]] = {}
    for index, airport in enumerate(airports):
        if not isinstance(airport, dict):
            raise ValueError(f"{AIRPORTS_PATH} record {index} is not an object")
        icao = str(airport.get("icao", "")).strip().upper()
        country = airport.get("country")
        expected_prefix = ALLOWED_COUNTRIES.get(country)
        if (
            expected_prefix
            and ICAO_PATTERN.fullmatch(icao)
            and icao.startswith(expected_prefix)
        ):
            if icao in allowed:
                raise ValueError(f"{AIRPORTS_PATH} contains duplicate ICAO {icao}")
            allowed[icao] = airport

    if not allowed:
        raise ValueError(f"{AIRPORTS_PATH} contains no allowed airports")
    return allowed


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
        current = now or datetime.now(timezone.utc)
        return max(
            0.0,
            (retry_at.astimezone(timezone.utc) - current.astimezone(timezone.utc)).total_seconds(),
        )
    except (TypeError, ValueError, OverflowError):
        return 0.0


def download_cache() -> bytes:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = urllib.request.Request(
            SOURCE_URL,
            headers={"Accept": "application/xml", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_SECONDS
            ) as response:
                status_value = getattr(response, "status", None)
                status = int(status_value if status_value is not None else response.getcode())
                payload = response.read()
                response_headers = getattr(response, "headers", None)
            if status != 200:
                if status in TRANSIENT_HTTP_CODES and attempt < MAX_ATTEMPTS:
                    delay = retry_after_seconds(
                        response_headers.get("Retry-After") if response_headers else None
                    )
                    if delay <= 0:
                        delay = min(30.0, 2 ** (attempt - 1))
                    print(
                        f"Temporary HTTP {status}; retry {attempt}/{MAX_ATTEMPTS - 1} "
                        f"in {delay:.1f}s",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                raise UpdateError(f"AWC METAR cache returned HTTP {status}")
            if not payload:
                raise UpdateError("AWC METAR cache returned an empty response")
            try:
                return gzip.decompress(payload) if payload.startswith(b"\x1f\x8b") else payload
            except (OSError, EOFError) as error:
                raise UpdateError(f"AWC METAR cache returned corrupt gzip: {error}") from error
        except urllib.error.HTTPError as error:
            retryable = error.code in TRANSIENT_HTTP_CODES
            if not retryable or attempt == MAX_ATTEMPTS:
                raise UpdateError(
                    f"AWC METAR cache request failed with HTTP {error.code} "
                    f"after {attempt} attempt(s)"
                ) from error
            headers = error.headers
            delay = retry_after_seconds(headers.get("Retry-After") if headers else None)
            if delay <= 0:
                delay = min(30.0, 2 ** (attempt - 1))
            print(
                f"Temporary HTTP {error.code}; retry {attempt}/{MAX_ATTEMPTS - 1} "
                f"in {delay:.1f}s",
                file=sys.stderr,
            )
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if attempt == MAX_ATTEMPTS:
                raise UpdateError(
                    f"AWC METAR cache request failed after {attempt} attempts: {error}"
                ) from error
            delay = min(30.0, 2 ** (attempt - 1))
            print(
                f"Temporary network error; retry {attempt}/{MAX_ATTEMPTS - 1} "
                f"in {delay:.1f}s",
                file=sys.stderr,
            )
            time.sleep(delay)
        except UpdateError:
            raise
    raise UpdateError("AWC METAR cache retries exhausted")


def parse_obs_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def parse_source(payload: bytes) -> ET.Element:
    if not payload or not payload.strip():
        raise UpdateError("AWC METAR cache response is empty")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise UpdateError(f"AWC METAR cache contains corrupt XML: {error}") from error
    if not list(iter_metars(root)):
        raise UpdateError("AWC METAR cache contains no METAR records")
    return root


def cloud_layers(node: ET.Element) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    clouds: List[Dict[str, Any]] = []
    ceiling_candidates: List[int] = []

    for sky in node.findall("sky_condition"):
        cover = (sky.get("sky_cover") or "").strip().upper()
        base_value = parse_float(sky.get("cloud_base_ft_agl"))
        base = round(base_value) if base_value is not None else None
        if cover:
            clouds.append({"cover": cover, "base": base})
        if cover in {"BKN", "OVC"} and base is not None:
            ceiling_candidates.append(base)

    vertical_value = parse_float(text(node, "vert_vis_ft"))
    if vertical_value is not None:
        vertical_base = round(vertical_value)
        clouds.append({"cover": "VV", "base": vertical_base})
        ceiling_candidates.append(vertical_base)

    ceiling = min(ceiling_candidates) if ceiling_candidates else None
    return clouds, ceiling


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


def normalize_metar(
    node: ET.Element, airport: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[str]]:
    visibility = parse_float(text(node, "visibility_statute_mi"))
    clouds, ceiling = cloud_layers(node)

    source_category = (text(node, "flight_category") or "").upper()
    flt_cat = (
        source_category
        if source_category in VALID_CATEGORIES
        else calculate_category(visibility, ceiling)
    )

    altimeter_in_hg = parse_float(text(node, "altim_in_hg"))
    altimeter_hpa = (
        round(altimeter_in_hg * 33.8638866667)
        if altimeter_in_hg is not None
        else None
    )

    source_lat = parse_float(text(node, "latitude"))
    source_lon = parse_float(text(node, "longitude"))
    lat = source_lat if source_lat is not None else parse_float(str(airport.get("lat")))
    lon = source_lon if source_lon is not None else parse_float(str(airport.get("lon")))

    observed_at = parse_obs_time(text(node, "observation_time"))
    if observed_at is None:
        raise UpdateError("missing or invalid observation_time")

    record: Dict[str, Any] = {
        "icaoId": str(airport["icao"]).upper(),
        "obsTime": iso_utc(observed_at),
        "rawOb": text(node, "raw_text"),
        "fltCat": flt_cat,
        "visib": compact_number(visibility),
        "wdir": compact_number(parse_float(text(node, "wind_dir_degrees"))),
        "wspd": compact_number(parse_float(text(node, "wind_speed_kt"))),
        "wgst": compact_number(parse_float(text(node, "wind_gust_kt"))),
        "altim": altimeter_hpa,
        "clouds": clouds,
        "ceiling": ceiling,
        "temp": compact_number(parse_float(text(node, "temp_c"))),
        "dewp": compact_number(parse_float(text(node, "dewpoint_c"))),
        "wxString": text(node, "wx_string"),
        "lat": lat,
        "lon": lon,
    }

    missing = [
        field
        for field in ("rawOb", "visib", "wdir", "wspd", "altim", "temp", "dewp")
        if record[field] is None
    ]
    if source_lat is None:
        missing.append("sourceLat")
    if source_lon is None:
        missing.append("sourceLon")
    return record, missing


def iter_metars(root: ET.Element) -> Iterable[ET.Element]:
    return root.findall(".//METAR")


def validate_record(
    record: Any,
    *,
    require_obs_time: bool = True,
    allowed_ids: Optional[set[str]] = None,
) -> Tuple[bool, str]:
    if not isinstance(record, dict):
        return False, "record is not an object"
    icao = str(record.get("icaoId", "")).strip().upper()
    if not ICAO_PATTERN.fullmatch(icao) or record.get("icaoId") != icao:
        return False, "invalid or non-canonical ICAO"
    if allowed_ids is not None and icao not in allowed_ids:
        return False, f"unrequested ICAO {icao}"
    raw = record.get("rawOb")
    if not isinstance(raw, str) or not raw.strip():
        return False, f"missing rawOb for {icao}"
    raw_match = RAW_METAR_PATTERN.match(raw.strip())
    if raw_match is None or raw_match.group(1).upper() != icao:
        return False, f"rawOb/ICAO mismatch for {icao}"
    obs_time = record.get("obsTime")
    if require_obs_time:
        parsed_time = parse_obs_time(obs_time)
        if parsed_time is None or obs_time != iso_utc(parsed_time):
            return False, f"invalid or non-canonical obsTime for {icao}"
    elif obs_time is not None:
        parsed_time = parse_obs_time(obs_time)
        if parsed_time is None or obs_time != iso_utc(parsed_time):
            return False, f"invalid optional obsTime for {icao}"
    category = record.get("fltCat")
    if not isinstance(category, str) or category not in VALID_CATEGORIES | {"UNKNOWN"}:
        return False, f"invalid flight category for {icao}"
    for field in NUMERIC_FIELDS:
        value = record.get(field)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            return False, f"invalid numeric field {field} for {icao}"
    if record.get("lat") is not None and not -90 <= float(record["lat"]) <= 90:
        return False, f"invalid latitude for {icao}"
    if record.get("lon") is not None and not -180 <= float(record["lon"]) <= 180:
        return False, f"invalid longitude for {icao}"
    for field in ("visib", "wspd", "wgst", "ceiling"):
        if record.get(field) is not None and float(record[field]) < 0:
            return False, f"negative {field} for {icao}"
    clouds = record.get("clouds")
    if not isinstance(clouds, list):
        return False, f"invalid clouds for {icao}"
    for layer in clouds:
        if not isinstance(layer, dict) or not isinstance(layer.get("cover"), str):
            return False, f"invalid cloud layer for {icao}"
        base = layer.get("base")
        if base is not None and (
            isinstance(base, bool)
            or not isinstance(base, (int, float))
            or not math.isfinite(float(base))
            or float(base) < 0
        ):
            return False, f"invalid cloud base for {icao}"
    return True, icao


def validate_dataset(
    document: Any,
    *,
    require_obs_time: bool = True,
    allowed_ids: Optional[set[str]] = None,
) -> Tuple[bool, str]:
    if not isinstance(document, list):
        return False, "root is not a JSON array"
    if not document:
        return False, "dataset is empty"
    seen: set[str] = set()
    for index, record in enumerate(document):
        valid, detail = validate_record(
            record, require_obs_time=require_obs_time, allowed_ids=allowed_ids
        )
        if not valid:
            return False, f"record {index}: {detail}"
        icao = str(record["icaoId"])
        if icao in seen:
            return False, f"duplicate ICAO {icao}"
        seen.add(icao)
    if [record["icaoId"] for record in document] != sorted(seen):
        return False, "records are not deterministically sorted by ICAO"
    return True, f"{len(document)} stations"


def load_previous_valid(path: Optional[Path] = None) -> Optional[List[Dict[str, Any]]]:
    target = path or OUTPUT_PATH
    try:
        with target.open(encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    try:
        valid, _ = validate_dataset(document, require_obs_time=False)
    except (TypeError, ValueError, OverflowError):
        return None
    return document if valid else None


def ensure_safe_record_count(
    records: Sequence[Mapping[str, Any]],
    previous_records: Optional[Sequence[Mapping[str, Any]]],
) -> None:
    new_count = len(records)
    if new_count == 0:
        raise UpdateError("empty METAR dataset refused")
    if not previous_records:
        return
    previous_count = len(previous_records)
    minimum = max(1, math.floor(previous_count * MINIMUM_STATION_RETENTION))
    if previous_count >= 10 and new_count < minimum:
        raise UpdateError(
            f"station count dropped unexpectedly from {previous_count} to {new_count} "
            f"(minimum accepted {minimum})"
        )


def fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def write_json(records: List[Dict[str, Any]], path: Optional[Path] = None) -> None:
    target = path or OUTPUT_PATH
    valid, detail = validate_dataset(records, require_obs_time=True)
    if not valid:
        raise UpdateError(f"refusing to publish invalid METAR dataset: {detail}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=".metar-live-",
            suffix=".json",
            delete=False,
        ) as handle:
            temp_name = handle.name
            json.dump(records, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, target)
        fsync_directory(target.parent)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def build_records(
    root: ET.Element, allowed: Mapping[str, Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], Counter[str], int, int, List[str]]:
    records_by_icao: Dict[str, Dict[str, Any]] = {}
    missing_fields: Counter[str] = Counter()
    ignored_without_icao = 0
    duplicate_matches = 0
    invalid_records: List[str] = []

    for index, node in enumerate(iter_metars(root)):
        icao = (text(node, "station_id") or "").upper()
        if not icao:
            ignored_without_icao += 1
            continue
        airport = allowed.get(icao)
        if airport is None:
            continue
        try:
            record, missing = normalize_metar(node, airport)
            valid, detail = validate_record(
                record, require_obs_time=True, allowed_ids=set(allowed)
            )
            if not valid:
                raise UpdateError(detail)
        except (TypeError, ValueError, UpdateError) as error:
            invalid_records.append(f"source record {index} ({icao}): {error}")
            continue
        previous = records_by_icao.get(icao)
        if previous is not None:
            duplicate_matches += 1
            previous_rank = (previous["obsTime"], previous["rawOb"])
            incoming_rank = (record["obsTime"], record["rawOb"])
            if incoming_rank <= previous_rank:
                continue
        records_by_icao[icao] = record
        missing_fields.update(missing)

    records = [records_by_icao[icao] for icao in sorted(records_by_icao)]
    return records, missing_fields, ignored_without_icao, duplicate_matches, invalid_records


def main() -> int:
    previous_records = load_previous_valid()
    try:
        allowed = load_allowed_airports()
        xml_payload = download_cache()
        root = parse_source(xml_payload)
        (
            records,
            missing_fields,
            ignored_without_icao,
            duplicate_matches,
            invalid_records,
        ) = build_records(root, allowed)
        valid, detail = validate_dataset(
            records, require_obs_time=True, allowed_ids=set(allowed)
        )
        if not valid:
            raise UpdateError(f"generated dataset failed validation: {detail}")
        ensure_safe_record_count(records, previous_records)
        write_json(records)
    except (OSError, ValueError, UpdateError, json.JSONDecodeError) as error:
        print(f"Update failed: {error}", file=sys.stderr)
        return 1

    categories = Counter(record["fltCat"] for record in records)
    print(f"Source: {SOURCE_URL}")
    print(f"Allowed airports: {len(allowed)}")
    print(f"METAR records in AWC cache: {len(list(iter_metars(root)))}")
    print(f"Matched unique airports: {len(records)}")
    print(f"Invalid matching source records ignored: {len(invalid_records)}")
    for warning in invalid_records[:10]:
        print(f"WARNING: {warning}", file=sys.stderr)
    print(
        "Categories: "
        + ", ".join(
            f"{category}={categories.get(category, 0)}"
            for category in ("VFR", "MVFR", "IFR", "LIFR", "UNKNOWN")
        )
    )
    print(f"Duplicate matching reports replaced: {duplicate_matches}")
    print(f"Cache reports without ICAO ignored: {ignored_without_icao}")
    print(
        "Missing optional/source fields: "
        + (
            ", ".join(f"{key}={value}" for key, value in sorted(missing_fields.items()))
            if missing_fields
            else "none"
        )
    )
    print(f"Output: {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
