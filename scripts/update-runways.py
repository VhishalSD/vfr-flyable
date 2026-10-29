#!/usr/bin/env python3
"""Safely build the local OurAirports runway subset."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from airport_magnetic import magnetic_heading


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AIRPORTS_PATH = PROJECT_ROOT / "data" / "airports-full.json"
OUTPUT_PATH = PROJECT_ROOT / "data" / "runways.json"
AIRPORT_METADATA_PATH = PROJECT_ROOT / "data" / "airport-metadata.json"
RUNWAYS_URL = "https://davidmegginson.github.io/ourairports-data/runways.csv"
USER_AGENT = "vfr-flyable-runway-updater/2.0 (+local-development)"
EXPECTED_AIRPORT_COUNT = 618
REQUEST_TIMEOUT_SECONDS = 90
MAX_ATTEMPTS = 3
ICAO_PATTERN = re.compile(r"^[A-Z]{4}$")
FEET_TO_METERS = 0.3048
METRIC_DECIMALS = 1
MINIMUM_LIVE_SOURCE_ROWS = 10_000
MINIMUM_EXISTING_RATIO = 0.8

SOURCE_FIELDS = {
    "id", "airport_ident", "length_ft", "width_ft", "surface", "lighted", "closed",
    "le_ident", "le_latitude_deg", "le_longitude_deg", "le_elevation_ft",
    "le_heading_degT", "le_displaced_threshold_ft", "he_ident", "he_latitude_deg",
    "he_longitude_deg", "he_elevation_ft", "he_heading_degT", "he_displaced_threshold_ft",
}

OUTPUT_FIELDS = (
    "id", "airport_ident", "le_ident", "he_ident", "length_ft", "length_m",
    "width_ft", "width_m", "surface", "lighted", "closed", "runwayPair",
    "le_latitude_deg", "le_longitude_deg", "le_elevation_ft", "le_heading_degT",
    "le_heading_degM", "le_displaced_threshold_ft", "he_latitude_deg", "he_longitude_deg",
    "he_elevation_ft", "he_heading_degT", "he_heading_degM", "he_displaced_threshold_ft",
)
PRE_MAGNETIC_OUTPUT_FIELDS = set(OUTPUT_FIELDS) - {"le_heading_degM", "he_heading_degM"}


class UpdateError(Exception):
    """Raised when a complete, safely publishable runway dataset cannot be built."""


def parse_number(value: Any, *, integer: bool = False) -> Optional[float | int]:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        raise UpdateError(f"invalid numeric source value {value!r}")
    if not math.isfinite(number):
        raise UpdateError(f"non-finite numeric source value {value!r}")
    if integer:
        if not number.is_integer():
            raise UpdateError(f"expected an integer source value, got {value!r}")
        return int(number)
    return int(number) if number.is_integer() else number


def parse_flag(value: Any, field: str) -> bool:
    normalized = str(value or "").strip()
    if normalized not in {"0", "1"}:
        raise UpdateError(f"invalid {field} flag {value!r}")
    return normalized == "1"


def optional_ident(value: Any) -> Optional[str]:
    return str(value or "").strip().upper() or None


def metric_value(feet: Optional[int]) -> Optional[float]:
    """Convert feet to metres, rounded to one decimal with Python's round()."""
    return round(feet * FEET_TO_METERS, METRIC_DECIMALS) if feet is not None else None


def load_allowed_airports(path: Path = AIRPORTS_PATH) -> Set[str]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UpdateError(f"cannot read a valid {path}: {error}") from error
    if not isinstance(document, list) or len(document) != EXPECTED_AIRPORT_COUNT:
        raise UpdateError(f"{path} must contain exactly {EXPECTED_AIRPORT_COUNT} airports")
    identifiers: Set[str] = set()
    for index, record in enumerate(document):
        if not isinstance(record, dict):
            raise UpdateError(f"airport #{index + 1} is not an object")
        icao = str(record.get("icao", "")).strip().upper()
        if not ICAO_PATTERN.fullmatch(icao) or icao in identifiers:
            raise UpdateError(f"invalid or duplicate ICAO {icao!r} in {path}")
        identifiers.add(icao)
    return identifiers


def load_magnetic_variations(
    allowed_airports: Set[str], path: Path = AIRPORT_METADATA_PATH
) -> Dict[str, Optional[float]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UpdateError(f"cannot read a valid magnetic variation dataset from {path}: {error}") from error
    records = document.get("airports") if isinstance(document, dict) else None
    if not isinstance(records, dict) or set(records) != allowed_airports:
        raise UpdateError("airport metadata does not contain exactly the expected airport set")
    variations: Dict[str, Optional[float]] = {}
    for icao in sorted(allowed_airports):
        record = records.get(icao)
        if not isinstance(record, dict):
            raise UpdateError(f"airport metadata record {icao} is invalid")
        value = record.get("signedDeclinationDeg")
        if value is None:
            variations[icao] = None
        elif (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise UpdateError(f"airport metadata record {icao} has invalid signed declination")
        else:
            variations[icao] = float(value)
    return variations


def download_runways_csv(url: str = RUNWAYS_URL) -> Tuple[str, List[str]]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/csv"})
    errors: List[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                if response.status != 200:
                    raise UpdateError(f"OurAirports returned HTTP {response.status}")
                payload = response.read()
            text = payload.decode("utf-8-sig")
            header = next(csv.reader(io.StringIO(text)), [])
            if not SOURCE_FIELDS.issubset(set(header)):
                raise UpdateError("response does not look like OurAirports runways.csv")
            return text, errors
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError, UpdateError) as error:
            errors.append(f"attempt {attempt}: {error}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(attempt * 2)
    raise UpdateError("failed to download OurAirports runways.csv; " + "; ".join(errors))


def read_source_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise UpdateError(f"cannot read runways fixture {path}: {error}") from error


def bounded_number(
    row: Mapping[str, str], field: str, *, minimum: Optional[float] = None,
    maximum: Optional[float] = None, integer: bool = False,
) -> Optional[float | int]:
    value = parse_number(row.get(field), integer=integer)
    if value is not None and (
        (minimum is not None and value < minimum) or (maximum is not None and value > maximum)
    ):
        raise UpdateError(f"{field} value {value!r} is outside its valid range")
    return value


def normalize_row(
    row: Mapping[str, str], line_number: int, signed_declination: Optional[float] = None
) -> Dict[str, Any]:
    try:
        airport_ident = str(row.get("airport_ident", "")).strip().upper()
        runway_id = bounded_number(row, "id", minimum=1, integer=True)
        if runway_id is None:
            raise UpdateError("missing runway id")
        le_ident = optional_ident(row.get("le_ident"))
        he_ident = optional_ident(row.get("he_ident"))
        length_ft = bounded_number(row, "length_ft", minimum=0, integer=True)
        width_ft = bounded_number(row, "width_ft", minimum=0, integer=True)
        le_heading_true = bounded_number(row, "le_heading_degT")
        he_heading_true = bounded_number(row, "he_heading_degT")
        return {
            "id": runway_id,
            "airport_ident": airport_ident,
            "le_ident": le_ident,
            "he_ident": he_ident,
            "length_ft": length_ft,
            "length_m": metric_value(length_ft),
            "width_ft": width_ft,
            "width_m": metric_value(width_ft),
            "surface": str(row.get("surface", "")).strip() or None,
            "lighted": parse_flag(row.get("lighted"), "lighted"),
            "closed": parse_flag(row.get("closed"), "closed"),
            "runwayPair": f"{le_ident}/{he_ident}" if le_ident and he_ident else None,
            "le_latitude_deg": bounded_number(row, "le_latitude_deg", minimum=-90, maximum=90),
            "le_longitude_deg": bounded_number(row, "le_longitude_deg", minimum=-180, maximum=180),
            "le_elevation_ft": bounded_number(row, "le_elevation_ft", integer=True),
            # Preserve finite OurAirports source headings verbatim. A very small
            # number of source rows use values just outside 0..360; these are
            # reported as source-quality warnings rather than silently wrapped.
            "le_heading_degT": le_heading_true,
            "le_heading_degM": magnetic_heading(le_heading_true, signed_declination),
            "le_displaced_threshold_ft": bounded_number(row, "le_displaced_threshold_ft", minimum=0, integer=True),
            "he_latitude_deg": bounded_number(row, "he_latitude_deg", minimum=-90, maximum=90),
            "he_longitude_deg": bounded_number(row, "he_longitude_deg", minimum=-180, maximum=180),
            "he_elevation_ft": bounded_number(row, "he_elevation_ft", integer=True),
            "he_heading_degT": he_heading_true,
            "he_heading_degM": magnetic_heading(he_heading_true, signed_declination),
            "he_displaced_threshold_ft": bounded_number(row, "he_displaced_threshold_ft", minimum=0, integer=True),
        }
    except UpdateError as error:
        raise UpdateError(f"invalid runways.csv row at line {line_number}: {error}") from error


def parse_runways_csv(
    text: str,
    allowed_airports: Set[str],
    variations: Optional[Mapping[str, Optional[float]]] = None,
    *,
    require_live_source_size: bool = False,
) -> Tuple[List[Dict[str, Any]], int]:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or not SOURCE_FIELDS.issubset(set(reader.fieldnames)):
        missing = sorted(SOURCE_FIELDS - set(reader.fieldnames or []))
        raise UpdateError(f"runways.csv is missing required columns: {', '.join(missing)}")
    records: List[Dict[str, Any]] = []
    source_row_count = 0
    for line_number, row in enumerate(reader, start=2):
        source_row_count += 1
        airport_ident = str(row.get("airport_ident", "")).strip().upper()
        if airport_ident in allowed_airports:
            records.append(normalize_row(row, line_number, (variations or {}).get(airport_ident)))
    if require_live_source_size and source_row_count < MINIMUM_LIVE_SOURCE_ROWS:
        raise UpdateError(f"runways.csv unexpectedly contains only {source_row_count} source rows")
    validate_runways(records, allowed_airports, variations=variations)
    return records, source_row_count


def validate_runways(
    records: Any,
    allowed_airports: Set[str],
    *,
    legacy: bool = False,
    variations: Optional[Mapping[str, Optional[float]]] = None,
) -> None:
    if not isinstance(records, list) or not records:
        raise UpdateError("runway output must be a non-empty JSON array")
    seen_ids: Set[int] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise UpdateError(f"runway record #{index + 1} is not an object")
        airport_ident = record.get("airport_ident")
        if airport_ident not in allowed_airports:
            raise UpdateError(f"runway record #{index + 1} is outside the airport set")
        if legacy:
            if not {"airport_ident", "le_ident", "he_ident", "closed", "length_ft"}.issubset(record):
                raise UpdateError(f"legacy runway record #{index + 1} has an invalid schema")
            if not isinstance(record["closed"], bool):
                raise UpdateError(f"legacy runway record #{index + 1} has invalid closed")
            if record["length_ft"] is not None and (
                not isinstance(record["length_ft"], int)
                or isinstance(record["length_ft"], bool)
                or record["length_ft"] < 0
            ):
                raise UpdateError(f"legacy runway record #{index + 1} has invalid length_ft")
            for field in ("le_ident", "he_ident"):
                if record[field] is not None and not isinstance(record[field], str):
                    raise UpdateError(f"legacy runway record #{index + 1} has invalid {field}")
            for field in ("le_heading_degT", "he_heading_degT"):
                value = record.get(field)
                if value is not None and (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                ):
                    raise UpdateError(f"legacy runway record #{index + 1} has invalid {field}")
            continue
        missing = set(OUTPUT_FIELDS) - set(record)
        if missing:
            raise UpdateError(f"runway record #{index + 1} is missing: {', '.join(sorted(missing))}")
        unexpected = set(record) - set(OUTPUT_FIELDS)
        if unexpected:
            raise UpdateError(
                f"runway record #{index + 1} has unexpected fields: {', '.join(sorted(unexpected))}"
            )
        runway_id = record["id"]
        if not isinstance(runway_id, int) or isinstance(runway_id, bool) or runway_id <= 0:
            raise UpdateError(f"runway record #{index + 1} has an invalid id")
        if runway_id in seen_ids:
            raise UpdateError(f"duplicate runway id: {runway_id}")
        seen_ids.add(runway_id)
        for flag in ("lighted", "closed"):
            if not isinstance(record[flag], bool):
                raise UpdateError(f"runway {runway_id} has an invalid {flag} value")
        for field in ("le_ident", "he_ident", "surface"):
            if record[field] is not None and (
                not isinstance(record[field], str) or not record[field].strip()
            ):
                raise UpdateError(f"runway {runway_id} has invalid {field}")
        expected_pair = f"{record['le_ident']}/{record['he_ident']}" if record["le_ident"] and record["he_ident"] else None
        if record["runwayPair"] != expected_pair:
            raise UpdateError(f"runway {runway_id} has an invalid runwayPair")
        for feet_field, metres_field in (("length_ft", "length_m"), ("width_ft", "width_m")):
            feet = record[feet_field]
            if feet is not None and (not isinstance(feet, int) or feet < 0):
                raise UpdateError(f"runway {runway_id} has invalid {feet_field}")
            if record[metres_field] != metric_value(feet):
                raise UpdateError(f"runway {runway_id} has inconsistent {metres_field}")
        numeric_rules = {
            "le_latitude_deg": (-90, 90, False),
            "he_latitude_deg": (-90, 90, False),
            "le_longitude_deg": (-180, 180, False),
            "he_longitude_deg": (-180, 180, False),
            "le_elevation_ft": (None, None, True),
            "he_elevation_ft": (None, None, True),
            "le_heading_degT": (None, None, False),
            "he_heading_degT": (None, None, False),
            "le_heading_degM": (0, 359, True),
            "he_heading_degM": (0, 359, True),
            "le_displaced_threshold_ft": (0, None, True),
            "he_displaced_threshold_ft": (0, None, True),
        }
        for field, (minimum, maximum, integer) in numeric_rules.items():
            value = record[field]
            if value is None:
                continue
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or (integer and not isinstance(value, int))
                or (minimum is not None and value < minimum)
                or (maximum is not None and value > maximum)
            ):
                raise UpdateError(f"runway {runway_id} has invalid {field}")
        if variations is not None:
            declination = variations.get(airport_ident)
            for prefix in ("le", "he"):
                expected_heading = magnetic_heading(record[f"{prefix}_heading_degT"], declination)
                if record[f"{prefix}_heading_degM"] != expected_heading:
                    raise UpdateError(f"runway {runway_id} has inconsistent {prefix}_heading_degM")


def load_existing_output(path: Path, allowed_airports: Set[str]) -> Optional[List[Dict[str, Any]]]:
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, list) or not document or not all(isinstance(row, dict) for row in document):
            raise UpdateError("existing runway output is not a non-empty object array")
        key_sets = {frozenset(row) for row in document}
        if all("id" not in row for row in document):
            validate_runways(document, allowed_airports, legacy=True)
        elif key_sets == {frozenset(PRE_MAGNETIC_OUTPUT_FIELDS)}:
            # Accept exactly the immediately preceding schema for one safe
            # migration, while still fully validating every existing field.
            migrated = [dict(row, le_heading_degM=None, he_heading_degM=None) for row in document]
            validate_runways(migrated, allowed_airports)
        else:
            validate_runways(document, allowed_airports)
    except (OSError, json.JSONDecodeError, UpdateError) as error:
        raise UpdateError(f"existing output {path} is invalid; refusing to overwrite it: {error}") from error
    return document


def atomic_write_json(path: Path, records: Sequence[Mapping[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
            suffix=".tmp", delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(records, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    return path.stat().st_size


def print_report(
    records: Sequence[Mapping[str, Any]], source_row_count: int, allowed_airports: Set[str],
    size: int, retry_errors: Sequence[str],
) -> None:
    covered = {record["airport_ident"] for record in records}
    nullable_fields = tuple(field for field in OUTPUT_FIELDS if field not in {"id", "airport_ident", "lighted", "closed"})
    missing = {field: sum(record[field] is None for record in records) for field in nullable_fields}
    print(f"source: {RUNWAYS_URL}")
    print(f"sourceRowCount: {source_row_count}")
    print(f"allowedAirportCount: {len(allowed_airports)}")
    print(f"coveredAirportCount: {len(covered)}")
    print(f"runwayRecordCount: {len(records)}")
    print(f"openRunwayCount: {sum(not record['closed'] for record in records)}")
    print(
        "runwayEndsWithMagneticHeading: "
        + str(sum(record[field] is not None for record in records for field in ("le_heading_degM", "he_heading_degM")))
    )
    print(
        "runwayEndsMissingMagneticDespiteTrue: "
        + str(sum(
            record[true_field] is not None and record[magnetic_field] is None
            for record in records
            for true_field, magnetic_field in (
                ("le_heading_degT", "le_heading_degM"),
                ("he_heading_degT", "he_heading_degM"),
            )
        ))
    )
    print(f"retryErrorCount: {len(retry_errors)}")
    for error in retry_errors:
        print(f"retryWarning: {error}")
    for field in nullable_fields:
        print(f"missing_{field}: {missing[field]}")
    unusual_headings = sum(
        heading is not None and not 0 <= heading <= 360
        for record in records
        for heading in (record["le_heading_degT"], record["he_heading_degT"])
    )
    print(f"outOfRangeSourceHeadingCount: {unusual_headings}")
    print(f"outputBytes: {size}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-file", type=Path, help="use a local runways.csv fixture")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH, help="output JSON path")
    args = parser.parse_args(argv)
    try:
        allowed_airports = load_allowed_airports()
        variations = load_magnetic_variations(allowed_airports)
        output_path = args.output.resolve()
        existing = load_existing_output(output_path, allowed_airports)
        retry_errors: List[str] = []
        if args.source_file:
            source = read_source_file(args.source_file)
            require_live_source_size = False
        else:
            source, retry_errors = download_runways_csv()
            require_live_source_size = True
        records, source_row_count = parse_runways_csv(
            source,
            allowed_airports,
            variations,
            require_live_source_size=require_live_source_size,
        )
        records.sort(key=lambda runway: (
            runway["airport_ident"], runway["le_ident"] or "", runway["he_ident"] or "", runway["id"]
        ))
        validate_runways(records, allowed_airports, variations=variations)
        if existing and len(records) < max(1, int(len(existing) * MINIMUM_EXISTING_RATIO)):
            raise UpdateError(f"refusing unexplained record-count drop from {len(existing)} to {len(records)}")
        size = atomic_write_json(output_path, records)
        print_report(records, source_row_count, allowed_airports, size, retry_errors)
        return 0
    except (UpdateError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
