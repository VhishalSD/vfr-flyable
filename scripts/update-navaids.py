#!/usr/bin/env python3
"""Build safe nearby-navaid lists for every airport-metadata origin."""

from __future__ import annotations

import argparse
import bisect
import csv
import io
import json
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from airport_geo import distance_and_bearing, haversine_km
from ourairports_backend import (
    UpdateError,
    atomic_write_json,
    download_csv,
    iso_utc,
    load_origins,
    optional_number,
    optional_text,
    read_csv_fixture,
    utc_now,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
METADATA_PATH = PROJECT_ROOT / "data" / "airport-metadata.json"
OUTPUT_PATH = PROJECT_ROOT / "data" / "airport-navaids.json"
SOURCE_URL = "https://davidmegginson.github.io/ourairports-data/navaids.csv"
DEFAULT_RADIUS_KM = 100.0
DEFAULT_MAX_RESULTS = 20
MINIMUM_LIVE_SOURCE_ROWS = 10_000
MINIMUM_EXISTING_RATIO = 0.8

SOURCE_FIELDS = {
    "id", "ident", "name", "type", "frequency_khz", "latitude_deg", "longitude_deg",
    "elevation_ft", "iso_country", "associated_airport",
}
NAVAID_FIELDS = {
    "id", "name", "ident", "type", "frequencyKhz", "latitude", "longitude",
    "elevationFt", "isoCountry", "associatedAirport", "distanceKm", "bearingDeg",
}


def normalize_navaid(row: Mapping[str, str], line_number: int) -> Optional[Dict[str, Any]]:
    try:
        source_id = optional_number(row.get("id"), integer=True)
        name = optional_text(row.get("name"))
        ident = optional_text(row.get("ident"), upper=True)
        navaid_type = optional_text(row.get("type"))
        latitude = optional_number(row.get("latitude_deg"))
        longitude = optional_number(row.get("longitude_deg"))
        if source_id is None or source_id <= 0 or name is None or ident is None or navaid_type is None:
            raise UpdateError("id, name, ident or type is missing")
        if latitude is None or longitude is None:
            return None
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise UpdateError("coordinates are out of range")
        return {
            "id": source_id,
            "name": name,
            "ident": ident,
            "type": navaid_type,
            "frequencyKhz": optional_number(row.get("frequency_khz")),
            "latitude": latitude,
            "longitude": longitude,
            "elevationFt": optional_number(row.get("elevation_ft"), integer=True),
            "isoCountry": optional_text(row.get("iso_country"), upper=True),
            "associatedAirport": optional_text(row.get("associated_airport"), upper=True),
        }
    except UpdateError as error:
        raise UpdateError(f"invalid navaids.csv row at line {line_number}: {error}") from error


def parse_navaids_csv(
    text: str, *, require_live_source_size: bool = False
) -> Tuple[List[Dict[str, Any]], int, int, int]:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or not SOURCE_FIELDS.issubset(set(reader.fieldnames)):
        missing = sorted(SOURCE_FIELDS - set(reader.fieldnames or []))
        raise UpdateError(f"navaids.csv is missing required columns: {', '.join(missing)}")
    by_id: Dict[int, Dict[str, Any]] = {}
    duplicate_count = 0
    missing_coordinates = 0
    source_count = 0
    for line_number, row in enumerate(reader, start=2):
        source_count += 1
        navaid = normalize_navaid(row, line_number)
        if navaid is None:
            missing_coordinates += 1
            continue
        previous = by_id.get(navaid["id"])
        if previous is None:
            by_id[navaid["id"]] = navaid
        elif previous == navaid:
            duplicate_count += 1
        else:
            raise UpdateError(f"conflicting duplicate navaid id {navaid['id']}")
    if require_live_source_size and source_count < MINIMUM_LIVE_SOURCE_ROWS:
        raise UpdateError(f"navaids.csv unexpectedly contains only {source_count} rows")
    if not by_id:
        raise UpdateError("navaids.csv contains no usable coordinate records")
    return list(by_id.values()), source_count, duplicate_count, missing_coordinates


def build_document(
    origins: Mapping[str, Mapping[str, Any]],
    navaids: Sequence[Mapping[str, Any]],
    generated_at: datetime,
    retrieved_at: datetime,
    *,
    radius_km: float = DEFAULT_RADIUS_KM,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> Dict[str, Any]:
    if not math.isfinite(radius_km) or radius_km <= 0 or max_results <= 0:
        raise UpdateError("radius/max-results are invalid")
    usable = sorted(navaids, key=lambda navaid: (navaid["latitude"], navaid["id"]))
    latitudes = [navaid["latitude"] for navaid in usable]
    output: Dict[str, Any] = {}
    total = 0
    latitude_margin = radius_km / 110.574
    for icao, origin in sorted(origins.items()):
        start = bisect.bisect_left(latitudes, origin["latitude"] - latitude_margin)
        end = bisect.bisect_right(latitudes, origin["latitude"] + latitude_margin)
        selected: List[Tuple[float, int, Dict[str, Any]]] = []
        for navaid in usable[start:end]:
            raw_distance = haversine_km(
                origin["latitude"], origin["longitude"], navaid["latitude"], navaid["longitude"]
            )
            if raw_distance > radius_km:
                continue
            distance, bearing = distance_and_bearing(
                origin["latitude"], origin["longitude"], navaid["latitude"], navaid["longitude"]
            )
            selected.append((raw_distance, navaid["id"], {**navaid, "distanceKm": distance, "bearingDeg": bearing}))
        selected.sort(key=lambda item: (item[0], item[1]))
        records = [item[2] for item in selected[:max_results]]
        output[icao] = {
            "icaoId": icao,
            "origin": dict(origin),
            "recordCount": len(records),
            "navaids": records,
        }
        total += len(records)
    document = {
        "schemaVersion": 1,
        "generatedAt": iso_utc(generated_at),
        "source": {
            "provider": "OurAirports",
            "url": SOURCE_URL,
            "retrievedAt": iso_utc(retrieved_at),
            "selection": {"radiusKm": radius_km, "maxResults": max_results},
            "distanceMethod": "haversine; IUGG mean Earth radius 6371.0088 km",
            "bearingMethod": "initial great-circle bearing",
            "rounding": {"distanceKm": "nearest 0.1 km, half-up", "bearingDeg": "nearest whole degree, half-up"},
        },
        "originCount": len(output),
        "recordCount": total,
        "airports": output,
    }
    validate_document(document, origins)
    return document


def valid_optional_number(value: Any) -> bool:
    return value is None or (
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    )


def validate_document(document: Any, origins: Mapping[str, Mapping[str, Any]]) -> None:
    if not isinstance(document, dict) or document.get("schemaVersion") != 1:
        raise UpdateError("invalid navaid root/schemaVersion")
    airports = document.get("airports")
    if not isinstance(airports, dict) or set(airports) != set(origins):
        raise UpdateError("navaid output does not contain the complete origin set")
    selection = document.get("source", {}).get("selection") if isinstance(document.get("source"), dict) else None
    if not isinstance(selection, dict):
        raise UpdateError("navaid selection metadata is missing")
    radius = selection.get("radiusKm")
    maximum = selection.get("maxResults")
    if not isinstance(radius, (int, float)) or radius <= 0 or not isinstance(maximum, int) or maximum <= 0:
        raise UpdateError("navaid selection metadata is invalid")
    total = 0
    for icao, group in airports.items():
        if not isinstance(group, dict) or group.get("icaoId") != icao:
            raise UpdateError(f"invalid navaid group {icao}")
        records = group.get("navaids")
        if not isinstance(records, list) or group.get("recordCount") != len(records) or len(records) > maximum:
            raise UpdateError(f"invalid navaid record count for {icao}")
        seen: set[int] = set()
        previous_sort: Optional[Tuple[float, int]] = None
        origin = origins[icao]
        for record in records:
            if not isinstance(record, dict) or set(record) != NAVAID_FIELDS:
                raise UpdateError(f"invalid navaid schema for {icao}")
            source_id = record["id"]
            if not isinstance(source_id, int) or source_id <= 0 or source_id in seen:
                raise UpdateError(f"duplicate/invalid navaid id for {icao}")
            seen.add(source_id)
            if not all(valid_optional_number(record[field]) for field in ("frequencyKhz", "latitude", "longitude", "elevationFt")):
                raise UpdateError(f"invalid navaid numeric data for {icao}")
            expected_distance, expected_bearing = distance_and_bearing(
                origin["latitude"], origin["longitude"], record["latitude"], record["longitude"]
            )
            if record["distanceKm"] != expected_distance or record["bearingDeg"] != expected_bearing or expected_distance > radius:
                raise UpdateError(f"incorrect navaid distance/bearing for {icao}")
            raw_distance = haversine_km(
                origin["latitude"], origin["longitude"], record["latitude"], record["longitude"]
            )
            sort_key = (raw_distance, source_id)
            if previous_sort is not None and sort_key < previous_sort:
                raise UpdateError(f"navaids are not distance-sorted for {icao}")
            previous_sort = sort_key
        total += len(records)
    if document.get("originCount") != len(origins) or document.get("recordCount") != total:
        raise UpdateError("navaid root counts are inconsistent")


def load_existing(path: Path, origins: Mapping[str, Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        validate_document(document, origins)
    except (OSError, json.JSONDecodeError, UpdateError) as error:
        raise UpdateError(f"existing output {path} is invalid; refusing to overwrite it: {error}") from error
    return document


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-file", type=Path)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--radius-km", type=float, default=DEFAULT_RADIUS_KM)
    parser.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS)
    args = parser.parse_args(argv)
    try:
        origins = load_origins(METADATA_PATH)
        output_path = args.output.resolve()
        existing = load_existing(output_path, origins)
        retry_errors: List[str] = []
        if args.source_file:
            text, retrieved_at = read_csv_fixture(args.source_file, "navaids.csv")
            require_live_size = False
        else:
            text, retrieved_at, retry_errors = download_csv(SOURCE_URL, "navaids.csv", sorted(SOURCE_FIELDS))
            require_live_size = True
        navaids, source_count, duplicate_count, missing_coordinates = parse_navaids_csv(
            text, require_live_source_size=require_live_size
        )
        document = build_document(
            origins,
            navaids,
            utc_now(),
            retrieved_at,
            radius_km=args.radius_km,
            max_results=args.max_results,
        )
        if existing and document["recordCount"] < int(existing["recordCount"] * MINIMUM_EXISTING_RATIO):
            raise UpdateError(
                f"refusing unexplained record-count drop from {existing['recordCount']} to {document['recordCount']}"
            )
        size = atomic_write_json(output_path, document)
        types = Counter(record["type"] for group in document["airports"].values() for record in group["navaids"])
        print(f"sourceRowCount: {source_count}")
        print(f"sourceDuplicateCount: {duplicate_count}")
        print(f"sourceMissingCoordinateCount: {missing_coordinates}")
        print(f"originCount: {document['originCount']}")
        print(f"navaidRecordCount: {document['recordCount']}")
        print(f"selectedTypeCounts: {dict(sorted(types.items()))}")
        print(f"retryErrorCount: {len(retry_errors)}")
        print(f"outputBytes: {size}")
        print(f"generatedAt: {document['generatedAt']}")
        return 0
    except (UpdateError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
