#!/usr/bin/env python3
"""Build a rolling 24-hour METAR history file from the official AWC API."""

from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


SOURCE_URL = "https://aviationweather.gov/api/data/metar"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIVE_PATH = PROJECT_ROOT / "data" / "metar-live.json"
OUTPUT_PATH = PROJECT_ROOT / "data" / "metar-history.json"

SOURCE_HOURS = 25
OUTPUT_HOURS = 24
BATCH_SIZE = 9
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 90
MAX_ATTEMPTS = 3
USER_AGENT = "vfr-flyable-school-project/1.0 (METAR history updater)"
ICAO_PATTERN = re.compile(r"^[A-Z]{4}$")
OBSERVATION_FIELDS = ("obsTime", "temp", "visib", "wspd", "wgst", "altim")


class UpdateError(RuntimeError):
    """Raised when a complete, safe history update cannot be produced."""


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


def parse_obs_time(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
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
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def valid_measurement(value: Any) -> bool:
    return value is None or (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def validate_document(
    document: Any, expected_ids: Optional[set[str]] = None
) -> Tuple[bool, str]:
    if not isinstance(document, dict):
        return False, "root is not a JSON object"
    if document.get("schemaVersion") != 1:
        return False, "schemaVersion is not 1"
    generated_at = parse_obs_time(document.get("generatedAt"))
    window_start = parse_obs_time(document.get("windowStart"))
    window_end = parse_obs_time(document.get("windowEnd"))
    if generated_at is None or window_start is None or window_end is None:
        return False, "generatedAt/window timestamps are invalid"
    if window_start >= window_end or generated_at != window_end:
        return False, "history window is inconsistent"
    if window_end - window_start != timedelta(hours=OUTPUT_HOURS):
        return False, f"history window is not {OUTPUT_HOURS} hours"

    airports = document.get("airports")
    if not isinstance(airports, dict) or not airports:
        return False, "airports is missing or empty"
    if expected_ids is not None and set(airports) != expected_ids:
        return False, "airport set does not match the current METAR station set"

    observation_count = 0
    for icao, station in airports.items():
        if not ICAO_PATTERN.fullmatch(icao) or not isinstance(station, dict):
            return False, f"invalid airport entry {icao!r}"
        if station.get("icaoId") != icao:
            return False, f"ICAO mismatch for {icao}"
        observations = station.get("observations")
        if not isinstance(observations, list):
            return False, f"observations for {icao} is not an array"
        previous_time: Optional[datetime] = None
        seen_times: set[str] = set()
        for record in observations:
            if not isinstance(record, dict) or set(record) != set(OBSERVATION_FIELDS):
                return False, f"invalid observation record for {icao}"
            observed_at = parse_obs_time(record.get("obsTime"))
            if observed_at is None:
                return False, f"invalid obsTime for {icao}"
            canonical_time = iso_utc(observed_at)
            if record.get("obsTime") != canonical_time:
                return False, f"non-canonical obsTime for {icao}"
            if canonical_time in seen_times:
                return False, f"duplicate obsTime for {icao}"
            if previous_time is not None and observed_at <= previous_time:
                return False, f"observations for {icao} are not chronological"
            if observed_at < window_start or observed_at > window_end:
                return False, f"observation for {icao} falls outside the history window"
            if not all(
                valid_measurement(record.get(field))
                for field in OBSERVATION_FIELDS[1:]
            ):
                return False, f"non-numeric measurement for {icao} at {canonical_time}"
            seen_times.add(canonical_time)
            previous_time = observed_at
            observation_count += 1
    return True, f"{len(airports)} stations, {observation_count} observations"


def load_existing_document(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    path = path or OUTPUT_PATH
    try:
        with path.open(encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise UpdateError(f"Existing history is unreadable; preserving it: {error}") from error

    valid, detail = validate_document(document)
    if not valid:
        raise UpdateError(f"Existing history is invalid; preserving it: {detail}")
    return document


def load_icao_ids() -> List[str]:
    try:
        with LIVE_PATH.open(encoding="utf-8") as handle:
            records = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise UpdateError(f"Cannot read {LIVE_PATH}: {error}") from error

    if not isinstance(records, list):
        raise UpdateError(f"{LIVE_PATH} must contain a JSON array")

    ids = {
        str(record.get("icaoId", "")).strip().upper()
        for record in records
        if isinstance(record, dict)
    }
    invalid = sorted(icao for icao in ids if icao and not ICAO_PATTERN.fullmatch(icao))
    if invalid:
        raise UpdateError(f"Invalid ICAO identifiers in {LIVE_PATH}: {', '.join(invalid)}")
    valid = sorted(icao for icao in ids if ICAO_PATTERN.fullmatch(icao))
    if not valid:
        raise UpdateError(f"No ICAO identifiers found in {LIVE_PATH}")
    return valid


def batches(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def request_url(icao_ids: Sequence[str]) -> str:
    query = urllib.parse.urlencode(
        {
            "ids": ",".join(icao_ids),
            "hours": SOURCE_HOURS,
            "format": "json",
        }
    )
    return f"{SOURCE_URL}?{query}"


def fetch_batch(icao_ids: Sequence[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    url = request_url(icao_ids)
    errors: List[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_SECONDS
            ) as response:
                payload = response.read()
            decoded = json.loads(payload.decode("utf-8"))
            if not isinstance(decoded, list):
                raise UpdateError(
                    f"AWC returned {type(decoded).__name__}, expected a JSON array"
                )
            records = [record for record in decoded if isinstance(record, dict)]
            if len(records) != len(decoded):
                raise UpdateError("AWC JSON array contains non-object records")
            return records, errors
        except urllib.error.HTTPError as error:
            if error.code == 204:
                return [], errors
            message = f"HTTP {error.code} for {','.join(icao_ids)}"
            errors.append(message)
            retryable = error.code == 429 or 500 <= error.code < 600
            if not retryable or attempt == MAX_ATTEMPTS:
                raise UpdateError(message) from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            message = f"Request failed for {','.join(icao_ids)}: {error}"
            errors.append(message)
            if attempt == MAX_ATTEMPTS:
                raise UpdateError(message) from error
        except UpdateError:
            raise

        time.sleep(2 ** (attempt - 1))

    raise UpdateError(f"Request failed for {','.join(icao_ids)}")


def normalize_altimeter(
    value: Any, detected_units: Counter[str]
) -> Optional[int]:
    number = parse_number(value)
    if number is None:
        return None

    # AWC v4 JSON currently returns altim in hPa. Keep compatibility with the
    # XML updater and defensively recognize inHg or Pa should the source vary.
    if 800 <= number <= 1100:
        detected_units["hPa"] += 1
        return round(number)
    if 20 <= number <= 35:
        detected_units["inHg"] += 1
        return round(number * 33.8638866667)
    if 80000 <= number <= 110000:
        detected_units["Pa"] += 1
        return round(number / 100)
    raise UpdateError(f"Unexpected AWC altim value: {value!r}")


def normalize_record(
    source: Dict[str, Any], detected_units: Counter[str]
) -> Optional[Tuple[str, datetime, Dict[str, Any]]]:
    icao = str(source.get("icaoId", "")).strip().upper()
    observed_at = parse_obs_time(source.get("obsTime"))
    if not ICAO_PATTERN.fullmatch(icao) or observed_at is None:
        return None

    record = {
        "obsTime": iso_utc(observed_at),
        "temp": compact_number(parse_number(source.get("temp"))),
        "visib": compact_number(parse_number(source.get("visib"))),
        "wspd": compact_number(parse_number(source.get("wspd"))),
        "wgst": compact_number(parse_number(source.get("wgst"))),
        "altim": normalize_altimeter(source.get("altim"), detected_units),
    }
    return icao, observed_at, record


def build_document(
    icao_ids: Sequence[str],
    source_records: Sequence[Dict[str, Any]],
    existing_document: Optional[Dict[str, Any]] = None,
    generated_at: Optional[datetime] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    generated_at = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    window_start = generated_at - timedelta(hours=OUTPUT_HOURS)
    detected_units: Counter[str] = Counter()
    deduplicated: Dict[Tuple[str, str], Tuple[datetime, Dict[str, Any]]] = {}
    ignored_invalid = 0
    ignored_unknown_station = 0
    ignored_outside_window = 0
    allowed = set(icao_ids)

    existing_input_count = 0
    if existing_document is not None:
        valid, detail = validate_document(existing_document)
        if not valid:
            raise UpdateError(f"Existing history cannot be merged: {detail}")
        for icao, station in existing_document["airports"].items():
            for record in station["observations"]:
                existing_input_count += 1
                if icao not in allowed:
                    continue
                observed_at = parse_obs_time(record["obsTime"])
                assert observed_at is not None
                if window_start <= observed_at <= generated_at:
                    deduplicated[(icao, record["obsTime"])] = (
                        observed_at,
                        dict(record),
                    )

    source_usable = 0
    source_new = 0
    source_updated = 0
    for source in source_records:
        normalized = normalize_record(source, detected_units)
        if normalized is None:
            ignored_invalid += 1
            continue
        icao, observed_at, record = normalized
        if icao not in allowed:
            ignored_unknown_station += 1
            continue
        if observed_at < window_start or observed_at > generated_at:
            ignored_outside_window += 1
            continue
        source_usable += 1
        key = (icao, record["obsTime"])
        previous = deduplicated.get(key)
        if previous is None:
            source_new += 1
        else:
            source_updated += 1
        # Prefer the most complete representation of an identical observation.
        # On a tie, the freshly downloaded AWC record wins deterministically.
        previous_score = (
            sum(value is not None for value in previous[1].values())
            if previous is not None
            else -1
        )
        new_score = sum(value is not None for value in record.values())
        if previous is None or new_score >= previous_score:
            deduplicated[key] = (observed_at, record)

    records_by_icao: Dict[str, List[Tuple[datetime, Dict[str, Any]]]] = {
        icao: [] for icao in icao_ids
    }
    for (icao, _), item in deduplicated.items():
        records_by_icao[icao].append(item)

    airports: Dict[str, Dict[str, Any]] = {}
    all_records: List[Dict[str, Any]] = []
    for icao in icao_ids:
        observations = [
            record
            for _, record in sorted(records_by_icao[icao], key=lambda item: item[0])
        ]
        all_records.extend(observations)
        airports[icao] = {"icaoId": icao, "observations": observations}

    missing = Counter(
        field
        for record in all_records
        for field in ("temp", "visib", "wspd", "altim")
        if record[field] is None
    )
    obs_times = [record["obsTime"] for record in all_records]
    successful_stations = sum(
        1 for station in airports.values() if station["observations"]
    )

    document = {
        "schemaVersion": 1,
        "generatedAt": iso_utc(generated_at),
        "windowStart": iso_utc(window_start),
        "windowEnd": iso_utc(generated_at),
        "source": {
            "provider": "NOAA Aviation Weather Center",
            "windowHours": OUTPUT_HOURS,
        },
        "units": {
            "temp": "C",
            "visib": "SM",
            "wspd": "kt",
            "wgst": "kt",
            "altim": "hPa",
        },
        "airports": airports,
    }
    stats = {
        "requestedStations": len(icao_ids),
        "successfulStations": successful_stations,
        "observations": len(all_records),
        "averagePerStation": (
            len(all_records) / len(icao_ids) if icao_ids else 0.0
        ),
        "oldestObsTime": min(obs_times) if obs_times else None,
        "newestObsTime": max(obs_times) if obs_times else None,
        "missing": {field: missing[field] for field in ("temp", "visib", "wspd", "altim")},
        "detectedAltimUnits": dict(detected_units),
        "ignoredInvalid": ignored_invalid,
        "ignoredUnknownStation": ignored_unknown_station,
        "ignoredOutsideWindow": ignored_outside_window,
        "existingInputObservations": existing_input_count,
        "retainedExistingObservations": max(0, len(all_records) - source_new),
        "sourceUsableObservations": source_usable,
        "sourceNewObservations": source_new,
        "sourceUpdatedObservations": source_updated,
    }
    return document, stats


def write_json_atomic(document: Dict[str, Any], path: Optional[Path] = None) -> int:
    path = path or OUTPUT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".metar-history-",
            suffix=".json",
            delete=False,
        ) as handle:
            temp_name = handle.name
            json.dump(document, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    return path.stat().st_size


def main() -> int:
    try:
        icao_ids = load_icao_ids()
        existing_document = load_existing_document()
        source_records: List[Dict[str, Any]] = []
        api_errors: List[str] = []
        request_batches = list(batches(icao_ids, BATCH_SIZE))

        for index, batch in enumerate(request_batches, start=1):
            print(
                f"Fetching batch {index}/{len(request_batches)} "
                f"({len(batch)} stations)...",
                file=sys.stderr,
            )
            records, errors = fetch_batch(batch)
            source_records.extend(records)
            api_errors.extend(errors)
            if index < len(request_batches):
                time.sleep(REQUEST_DELAY_SECONDS)

        if not source_records:
            raise UpdateError("AWC returned no observations; existing history was preserved")
        document, stats = build_document(
            icao_ids, source_records, existing_document=existing_document
        )
        if stats["sourceUsableObservations"] == 0:
            raise UpdateError("AWC returned no usable observations; existing history was preserved")
        valid, detail = validate_document(document, expected_ids=set(icao_ids))
        if not valid:
            raise UpdateError(f"Generated history failed validation: {detail}")
        file_size = write_json_atomic(document)
    except (OSError, UpdateError) as error:
        print(f"Update failed: {error}", file=sys.stderr)
        return 1

    print(f"Source: {SOURCE_URL}")
    print(f"Requested ICAO stations: {stats['requestedStations']}")
    print(f"Successful stations: {stats['successfulStations']}")
    print(f"Historical observations: {stats['observations']}")
    print(f"Existing observations read: {stats['existingInputObservations']}")
    print(f"Existing observations retained: {stats['retainedExistingObservations']}")
    print(f"New observations added: {stats['sourceNewObservations']}")
    print(f"Existing timestamps refreshed: {stats['sourceUpdatedObservations']}")
    print(f"Average per requested station: {stats['averagePerStation']:.2f}")
    print(f"Oldest obsTime: {stats['oldestObsTime'] or 'none'}")
    print(f"Newest obsTime: {stats['newestObsTime'] or 'none'}")
    print(f"Output size: {file_size} bytes")
    print(
        "Missing values: "
        + ", ".join(f"{key}={value}" for key, value in stats["missing"].items())
    )
    print(
        "Detected AWC altim units: "
        + (
            ", ".join(
                f"{key}={value}"
                for key, value in sorted(stats["detectedAltimUnits"].items())
            )
            or "none"
        )
    )
    print(f"Ignored invalid records: {stats['ignoredInvalid']}")
    print(f"Ignored unknown stations: {stats['ignoredUnknownStation']}")
    print(f"Ignored outside 24-hour window: {stats['ignoredOutsideWindow']}")
    print(
        "HTTP/API errors: "
        + ("; ".join(api_errors) if api_errors else "none")
    )
    print(f"Output: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
