#!/usr/bin/env python3
"""Validate cross-dataset integrity for the complete Airport-tab backend."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from airport_time import validated_zoneinfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
METADATA_PATH = PROJECT_ROOT / "data" / "airport-metadata.json"
RUNWAYS_PATH = PROJECT_ROOT / "data" / "runways.json"
NEARBY_PATH = PROJECT_ROOT / "data" / "airport-nearby.json"
NAVAIDS_PATH = PROJECT_ROOT / "data" / "airport-navaids.json"
EXPECTED_AIRPORT_COUNT = 618
ICAO_PATTERN = re.compile(r"^[A-Z]{4}$")


class ValidationError(RuntimeError):
    """Raised when an Airport backend invariant is broken."""


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"cannot read valid JSON from {path}: {error}") from error


def finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def coordinate(value: Any, minimum: float, maximum: float, context: str) -> None:
    if not finite_number(value) or not minimum <= value <= maximum:
        raise ValidationError(f"{context} is outside {minimum}..{maximum}")


def nonempty_text(value: Any, context: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{context} is missing")


def validate_metadata(document: Any) -> tuple[set[str], dict[str, Mapping[str, Any]]]:
    if not isinstance(document, dict) or document.get("schemaVersion") != 1:
        raise ValidationError("airport metadata root/schemaVersion is invalid")
    airports = document.get("airports")
    if not isinstance(airports, dict) or len(airports) != EXPECTED_AIRPORT_COUNT:
        raise ValidationError(f"airport metadata must contain exactly {EXPECTED_AIRPORT_COUNT} records")
    source_ids: set[int] = set()
    for icao, record in airports.items():
        if not ICAO_PATTERN.fullmatch(str(icao)) or not isinstance(record, dict) or record.get("icaoId") != icao:
            raise ValidationError(f"invalid airport metadata record {icao!r}")
        for field in ("name", "countryName", "regionName", "fir", "timezone"):
            nonempty_text(record.get(field), f"{icao}.{field}")
        validated_zoneinfo(record["timezone"])
        if not finite_number(record.get("signedDeclinationDeg")):
            raise ValidationError(f"{icao}.signedDeclinationDeg is missing/invalid")
        if not finite_number(record.get("magneticVariationDeg")):
            raise ValidationError(f"{icao}.magneticVariationDeg is missing/invalid")
        if record.get("magneticVariationDirection") not in {"E", "W", None}:
            raise ValidationError(f"{icao}.magneticVariationDirection is invalid")
        if not isinstance(record.get("magneticVariationYear"), int):
            raise ValidationError(f"{icao}.magneticVariationYear is missing/invalid")
        source_id = record.get("ourAirportsId")
        if not isinstance(source_id, int) or isinstance(source_id, bool) or source_id <= 0:
            raise ValidationError(f"{icao}.ourAirportsId is missing/invalid")
        if source_id in source_ids:
            raise ValidationError(f"duplicate OurAirports id {source_id}")
        source_ids.add(source_id)
        source_coordinates = record.get("sourceCoordinates")
        if not isinstance(source_coordinates, dict):
            raise ValidationError(f"{icao}.sourceCoordinates is missing")
        coordinate(source_coordinates.get("latitude"), -90, 90, f"{icao}.latitude")
        coordinate(source_coordinates.get("longitude"), -180, 180, f"{icao}.longitude")
        daylight = record.get("daylight")
        if not isinstance(daylight, dict) or daylight.get("state") not in {
            "normal", "polarDay", "polarNight", "unavailable"
        }:
            raise ValidationError(f"{icao}.daylight is invalid")
    return set(airports), airports


def validate_runways(records: Any, known_icaos: set[str]) -> dict[str, int]:
    if not isinstance(records, list):
        raise ValidationError("runways root must be an array")
    seen_ids: set[int] = set()
    covered: set[str] = set()
    magnetic_ends = 0
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValidationError(f"runway #{index + 1} is not an object")
        airport_ident = record.get("airport_ident")
        if airport_ident not in known_icaos:
            raise ValidationError(f"runway #{index + 1} references unknown airport {airport_ident!r}")
        covered.add(airport_ident)
        runway_id = record.get("id")
        if not isinstance(runway_id, int) or isinstance(runway_id, bool) or runway_id <= 0:
            raise ValidationError(f"runway #{index + 1} has invalid id")
        if runway_id in seen_ids:
            raise ValidationError(f"duplicate runway id {runway_id}")
        seen_ids.add(runway_id)
        for prefix in ("le", "he"):
            true_heading = record.get(f"{prefix}_heading_degT")
            magnetic_heading = record.get(f"{prefix}_heading_degM")
            if magnetic_heading is not None and true_heading is None:
                raise ValidationError(
                    f"runway {runway_id} has {prefix} magnetic heading without true heading"
                )
            if true_heading is not None:
                if not finite_number(true_heading):
                    raise ValidationError(f"runway {runway_id} has invalid {prefix} true heading")
                if magnetic_heading is None:
                    raise ValidationError(f"runway {runway_id} is missing {prefix} magnetic heading")
            if magnetic_heading is not None:
                if not isinstance(magnetic_heading, int) or isinstance(magnetic_heading, bool) or not 0 <= magnetic_heading <= 359:
                    raise ValidationError(f"runway {runway_id} has invalid {prefix} magnetic heading")
                magnetic_ends += 1
            latitude = record.get(f"{prefix}_latitude_deg")
            longitude = record.get(f"{prefix}_longitude_deg")
            if (latitude is None) != (longitude is None):
                raise ValidationError(f"runway {runway_id} has incomplete {prefix} coordinates")
            if latitude is not None:
                coordinate(latitude, -90, 90, f"runway {runway_id}.{prefix}_latitude")
                coordinate(longitude, -180, 180, f"runway {runway_id}.{prefix}_longitude")
    return {"runwayRecordCount": len(records), "runwayAirportCount": len(covered), "magneticRunwayEndCount": magnetic_ends}


def validate_indexed_geodata(
    document: Any,
    known_icaos: set[str],
    *,
    list_field: str,
    id_field: str,
    label: str,
) -> dict[str, int]:
    if not isinstance(document, dict) or document.get("schemaVersion") != 1:
        raise ValidationError(f"{label} root/schemaVersion is invalid")
    airports = document.get("airports")
    if not isinstance(airports, dict) or set(airports) != known_icaos:
        raise ValidationError(f"{label} index must contain exactly the known origin ICAOs")
    total = 0
    populated = 0
    for icao, group in airports.items():
        if not isinstance(group, dict) or group.get("icaoId") != icao:
            raise ValidationError(f"{label} group {icao} is invalid")
        records = group.get(list_field)
        if not isinstance(records, list) or group.get("recordCount") != len(records):
            raise ValidationError(f"{label} count for {icao} is invalid")
        seen: set[int] = set()
        for record in records:
            if not isinstance(record, dict):
                raise ValidationError(f"{label} record for {icao} is invalid")
            source_id = record.get(id_field)
            if not isinstance(source_id, int) or isinstance(source_id, bool) or source_id <= 0:
                raise ValidationError(f"{label} record for {icao} has invalid id")
            if source_id in seen:
                raise ValidationError(f"duplicate {label} id {source_id} for {icao}")
            seen.add(source_id)
            distance = record.get("distanceKm")
            bearing = record.get("bearingDeg")
            if not finite_number(distance) or distance < 0:
                raise ValidationError(f"{label} record for {icao} has invalid distance")
            if not isinstance(bearing, int) or isinstance(bearing, bool) or not 0 <= bearing <= 359:
                raise ValidationError(f"{label} record for {icao} has invalid bearing")
            coordinate(record.get("latitude"), -90, 90, f"{label} latitude for {icao}")
            coordinate(record.get("longitude"), -180, 180, f"{label} longitude for {icao}")
        total += len(records)
        populated += bool(records)
    if document.get("originCount") != len(known_icaos) or document.get("recordCount") != total:
        raise ValidationError(f"{label} root counts are inconsistent")
    return {f"{label}RecordCount": total, f"{label}PopulatedAirportCount": populated}


def validate_backend(metadata: Any, runways: Any, nearby: Any, navaids: Any) -> dict[str, int]:
    known_icaos, _ = validate_metadata(metadata)
    report = {"metadataRecordCount": len(known_icaos)}
    report.update(validate_runways(runways, known_icaos))
    report.update(
        validate_indexed_geodata(
            nearby, known_icaos, list_field="nearby", id_field="ourAirportsId", label="nearby"
        )
    )
    report.update(
        validate_indexed_geodata(
            navaids, known_icaos, list_field="navaids", id_field="id", label="navaid"
        )
    )
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, default=METADATA_PATH)
    parser.add_argument("--runways", type=Path, default=RUNWAYS_PATH)
    parser.add_argument("--nearby", type=Path, default=NEARBY_PATH)
    parser.add_argument("--navaids", type=Path, default=NAVAIDS_PATH)
    args = parser.parse_args(argv)
    try:
        report = validate_backend(
            load_json(args.metadata), load_json(args.runways),
            load_json(args.nearby), load_json(args.navaids),
        )
        print("airportBackendIntegrity: PASS")
        for key, value in report.items():
            print(f"{key}: {value}")
        return 0
    except (ValidationError, ValueError) as error:
        print(f"airportBackendIntegrity: FAIL: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
