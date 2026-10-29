#!/usr/bin/env python3
"""Build airport metadata from OurAirports, FIR polygons and offline calculations."""

from __future__ import annotations

import argparse
import copy
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
from collections import Counter, defaultdict
from datetime import datetime, timezone
from importlib import metadata as package_metadata
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from timezonefinder import TimezoneFinder

from airport_time import calculate_daylight, local_date, validated_zoneinfo
from airport_magnetic import (
    MODEL_NAME as MAGNETIC_MODEL_NAME,
    MODEL_SOURCE as MAGNETIC_MODEL_SOURCE,
    MagneticModelError,
    calculate_signed_declination,
    variation_fields,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
AIRPORTS_PATH = PROJECT_ROOT / "data" / "airports-full.json"
OUTPUT_PATH = PROJECT_ROOT / "data" / "airport-metadata.json"
OURAIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
OURAIRPORTS_COUNTRIES_URL = "https://davidmegginson.github.io/ourairports-data/countries.csv"
OURAIRPORTS_REGIONS_URL = "https://davidmegginson.github.io/ourairports-data/regions.csv"
FIR_BOUNDARIES_URL = (
    "https://raw.githubusercontent.com/Transport-Environment/"
    "atm_and_contrail_avoidance/main/data/worldfirs.geojson"
)
FIR_ORIGINAL_URL = "https://observablehq.com/@openaviation/flight-information-regions"
FIR_EUROPE_REFERENCE_URL = (
    "https://www.eurocontrol.int/publication/flight-information-region-firuir-charts"
)
FIR_PROVIDER = "Open Aviation FIR boundaries"
USER_AGENT = "vfr-flyable-airport-metadata-updater/1.0 (+local-development)"
EXPECTED_AIRPORT_COUNT = 618
REQUEST_TIMEOUT_SECONDS = 90
MAX_ATTEMPTS = 3
MAX_COORDINATE_DISTANCE_KM = 20.0
ICAO_PATTERN = re.compile(r"^[A-Z]{4}$")
IATA_PATTERN = re.compile(r"^[A-Z]{3}$")
COUNTRY_CODE_PATTERN = re.compile(r"^[A-Z]{2}$")
REGION_CODE_PATTERN = re.compile(r"^[A-Z]{2}-.+$")
FIR_DESIGNATOR_PATTERN = re.compile(r"^[A-Z0-9]{4}$")
# Integrity sentinels for every country represented by airports-full.json. These
# validate the downloaded polygon source; they are never used to assign a FIR.
REQUIRED_REFERENCE_FIR_DESIGNATORS = {"EBBU", "EHAA", "EGTT", "LFFF", "EDMM"}
COUNTRY_TO_ISO = {
    "Belgium": "BE",
    "France": "FR",
    "Germany": "DE",
    "Netherlands": "NL",
    "United Kingdom": "GB",
}


class UpdateError(Exception):
    """Raised when a complete, safely writable update cannot be produced."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("missing ISO timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp is not timezone-aware: {value!r}")
    return parsed.astimezone(timezone.utc)


def parse_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def compact_number(value: Optional[float], digits: int = 4) -> Optional[float]:
    if value is None:
        return None
    if float(value).is_integer():
        return int(value)
    return round(value, digits)


def load_airports(path: Path = AIRPORTS_PATH) -> List[Dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise UpdateError(f"cannot read a valid {path}: {error}") from error
    if not isinstance(document, list):
        raise UpdateError(f"{path} must contain a JSON array")
    if len(document) != EXPECTED_AIRPORT_COUNT:
        raise UpdateError(
            f"expected {EXPECTED_AIRPORT_COUNT} airports in {path}, found {len(document)}"
        )

    seen = set()
    normalized: List[Dict[str, Any]] = []
    for index, item in enumerate(document):
        if not isinstance(item, dict):
            raise UpdateError(f"airport #{index + 1} is not an object")
        icao = str(item.get("icao", "")).strip().upper()
        if not ICAO_PATTERN.fullmatch(icao):
            raise UpdateError(f"airport #{index + 1} has invalid ICAO {icao!r}")
        if icao in seen:
            raise UpdateError(f"duplicate ICAO in source dataset: {icao}")
        seen.add(icao)
        latitude = parse_number(item.get("lat"))
        longitude = parse_number(item.get("lon"))
        if latitude is None or longitude is None or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise UpdateError(f"{icao} has invalid coordinates")
        country = str(item.get("country", "")).strip()
        if country not in COUNTRY_TO_ISO:
            raise UpdateError(f"{icao} has unsupported country value {country!r}")
        normalized.append(
            {
                "icao": icao,
                "country": country,
                "latitude": latitude,
                "longitude": longitude,
            }
        )
    return normalized


def download_ourairports_csv(
    url: str,
    label: str,
    required_columns: Sequence[str],
) -> Tuple[str, datetime]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/csv"})
    errors: List[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                if response.status != 200:
                    raise UpdateError(f"OurAirports {label} returned HTTP {response.status}")
                payload = response.read()
            text = payload.decode("utf-8-sig")
            header = next(csv.reader(io.StringIO(text)), [])
            if not set(required_columns).issubset(set(header)):
                raise UpdateError(f"OurAirports response does not look like {label}")
            return text, utc_now()
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError, UpdateError) as error:
            errors.append(f"attempt {attempt}: {error}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(attempt * 2)
    raise UpdateError(f"failed to download OurAirports {label}; " + "; ".join(errors))


def download_airports_csv(url: str = OURAIRPORTS_URL) -> Tuple[str, datetime]:
    return download_ourairports_csv(url, "airports.csv", ("icao_code", "latitude_deg"))


def download_countries_csv(url: str = OURAIRPORTS_COUNTRIES_URL) -> Tuple[str, datetime]:
    return download_ourairports_csv(url, "countries.csv", ("code", "name"))


def download_regions_csv(url: str = OURAIRPORTS_REGIONS_URL) -> Tuple[str, datetime]:
    return download_ourairports_csv(url, "regions.csv", ("code", "name", "iso_country"))


def download_fir_geojson(url: str = FIR_BOUNDARIES_URL) -> Tuple[str, datetime]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json, application/json"},
    )
    errors: List[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                if response.status != 200:
                    raise UpdateError(f"FIR boundary source returned HTTP {response.status}")
                payload = response.read()
            text = payload.decode("utf-8")
            if '"FeatureCollection"' not in text[:500]:
                raise UpdateError("FIR response does not look like GeoJSON")
            return text, utc_now()
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError, UpdateError) as error:
            errors.append(f"attempt {attempt}: {error}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(attempt * 2)
    raise UpdateError("failed to download FIR boundary GeoJSON; " + "; ".join(errors))


def read_source_csv(path: Path, label: str = "OurAirports CSV") -> Tuple[str, datetime]:
    try:
        return path.read_text(encoding="utf-8-sig"), utc_now()
    except OSError as error:
        raise UpdateError(f"cannot read {label} fixture {path}: {error}") from error


def read_fir_source(path: Path) -> Tuple[str, datetime]:
    try:
        return path.read_text(encoding="utf-8"), utc_now()
    except OSError as error:
        raise UpdateError(f"cannot read FIR GeoJSON source {path}: {error}") from error


def parse_ourairports(text: str) -> List[Dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    required = {
        "id",
        "ident",
        "type",
        "name",
        "icao_code",
        "gps_code",
        "latitude_deg",
        "longitude_deg",
        "elevation_ft",
        "iso_country",
        "iso_region",
        "municipality",
        "iata_code",
        "scheduled_service",
        "local_code",
    }
    if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
        missing = sorted(required - set(reader.fieldnames or []))
        raise UpdateError(f"OurAirports CSV is missing required columns: {', '.join(missing)}")
    rows = [dict(row) for row in reader]
    if len(rows) < 1000:
        raise UpdateError(f"OurAirports CSV unexpectedly contains only {len(rows)} rows")
    return rows


def parse_countries(text: str) -> Dict[str, Dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    required = {"code", "name"}
    if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
        missing = sorted(required - set(reader.fieldnames or []))
        raise UpdateError(f"OurAirports countries.csv is missing columns: {', '.join(missing)}")
    countries: Dict[str, Dict[str, str]] = {}
    for index, row in enumerate(reader, start=2):
        code = str(row.get("code", "")).strip().upper()
        name = str(row.get("name", "")).strip()
        if not COUNTRY_CODE_PATTERN.fullmatch(code) or not name:
            raise UpdateError(f"invalid country row at line {index}")
        if code in countries:
            raise UpdateError(f"duplicate country code in countries.csv: {code}")
        countries[code] = dict(row)
    if not countries:
        raise UpdateError("OurAirports countries.csv contains no records")
    return countries


def parse_regions(text: str) -> Dict[str, Dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    required = {"code", "name", "iso_country"}
    if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
        missing = sorted(required - set(reader.fieldnames or []))
        raise UpdateError(f"OurAirports regions.csv is missing columns: {', '.join(missing)}")
    regions: Dict[str, Dict[str, str]] = {}
    for index, row in enumerate(reader, start=2):
        code = str(row.get("code", "")).strip().upper()
        name = str(row.get("name", "")).strip()
        country = str(row.get("iso_country", "")).strip().upper()
        if not REGION_CODE_PATTERN.fullmatch(code) or not COUNTRY_CODE_PATTERN.fullmatch(country) or not name:
            raise UpdateError(f"invalid region row at line {index}")
        if code in regions:
            raise UpdateError(f"duplicate region code in regions.csv: {code}")
        regions[code] = dict(row)
    if not regions:
        raise UpdateError("OurAirports regions.csv contains no records")
    return regions


def parse_position(value: Any, context: str) -> Tuple[float, float]:
    if not isinstance(value, list) or len(value) < 2:
        raise UpdateError(f"{context} contains an invalid coordinate")
    longitude = parse_number(value[0])
    latitude = parse_number(value[1])
    if (
        longitude is None
        or latitude is None
        or not -180 <= longitude <= 180
        or not -90 <= latitude <= 90
    ):
        raise UpdateError(f"{context} contains an out-of-range coordinate")
    return longitude, latitude


def parse_polygon_coordinates(value: Any, context: str) -> List[List[List[Tuple[float, float]]]]:
    if not isinstance(value, list) or not value:
        raise UpdateError(f"{context} has no polygon coordinates")
    polygons: List[List[List[Tuple[float, float]]]] = []
    for polygon_index, polygon in enumerate(value):
        if not isinstance(polygon, list) or not polygon:
            raise UpdateError(f"{context} polygon #{polygon_index + 1} has no rings")
        parsed_rings: List[List[Tuple[float, float]]] = []
        for ring_index, ring in enumerate(polygon):
            if not isinstance(ring, list) or len(ring) < 4:
                raise UpdateError(
                    f"{context} polygon #{polygon_index + 1} ring #{ring_index + 1} is invalid"
                )
            parsed_ring = [
                parse_position(position, context)
                for position in ring
            ]
            if parsed_ring[0] != parsed_ring[-1]:
                raise UpdateError(f"{context} contains an unclosed polygon ring")
            parsed_rings.append(parsed_ring)
        polygons.append(parsed_rings)
    return polygons


def parse_fir_boundaries(text: str) -> List[Dict[str, Any]]:
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise UpdateError(f"FIR source is not valid JSON: {error}") from error
    if not isinstance(document, dict) or document.get("type") != "FeatureCollection":
        raise UpdateError("FIR source root must be a GeoJSON FeatureCollection")
    source_features = document.get("features")
    if not isinstance(source_features, list) or not source_features:
        raise UpdateError("FIR source contains no features")

    features: List[Dict[str, Any]] = []
    seen_designators = set()
    for index, feature in enumerate(source_features):
        context = f"FIR feature #{index + 1}"
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise UpdateError(f"{context} is not a GeoJSON Feature")
        properties = feature.get("properties")
        geometry = feature.get("geometry")
        if not isinstance(properties, dict) or not isinstance(geometry, dict):
            raise UpdateError(f"{context} has missing properties or geometry")
        designator = str(properties.get("designator", "")).strip().upper()
        name = str(properties.get("name", "")).strip()
        feature_type = str(properties.get("type", "")).strip().upper()
        if not FIR_DESIGNATOR_PATTERN.fullmatch(designator):
            raise UpdateError(f"{context} has invalid designator {designator!r}")
        if feature_type != "FIR" or not name or "FIR" not in name.upper():
            raise UpdateError(f"{context} is not a named FIR")
        if designator in seen_designators:
            raise UpdateError(f"duplicate FIR designator in source: {designator}")
        seen_designators.add(designator)

        geometry_type = geometry.get("type")
        coordinates = geometry.get("coordinates")
        if geometry_type == "Polygon":
            polygons = parse_polygon_coordinates([coordinates], context)
        elif geometry_type == "MultiPolygon":
            polygons = parse_polygon_coordinates(coordinates, context)
        else:
            raise UpdateError(f"{context} has unsupported geometry type {geometry_type!r}")
        points = [point for polygon in polygons for ring in polygon for point in ring]
        longitudes = [point[0] for point in points]
        latitudes = [point[1] for point in points]
        features.append(
            {
                "designator": designator,
                "name": name,
                "polygons": polygons,
                "bbox": (min(longitudes), min(latitudes), max(longitudes), max(latitudes)),
            }
        )

    missing_references = REQUIRED_REFERENCE_FIR_DESIGNATORS - seen_designators
    if missing_references:
        raise UpdateError(
            "FIR source is incomplete; missing reference designators: "
            + ", ".join(sorted(missing_references))
        )
    return features


def point_on_segment(
    longitude: float,
    latitude: float,
    start: Tuple[float, float],
    end: Tuple[float, float],
    tolerance: float = 1e-10,
) -> bool:
    x1, y1 = start
    x2, y2 = end
    cross = (longitude - x1) * (y2 - y1) - (latitude - y1) * (x2 - x1)
    if abs(cross) > tolerance:
        return False
    return (
        min(x1, x2) - tolerance <= longitude <= max(x1, x2) + tolerance
        and min(y1, y2) - tolerance <= latitude <= max(y1, y2) + tolerance
    )


def point_in_ring(longitude: float, latitude: float, ring: Sequence[Tuple[float, float]]) -> bool:
    inside = False
    for index, end in enumerate(ring):
        start = ring[index - 1]
        if point_on_segment(longitude, latitude, start, end):
            return True
        x1, y1 = start
        x2, y2 = end
        if (y1 > latitude) != (y2 > latitude):
            intersection = (x2 - x1) * (latitude - y1) / (y2 - y1) + x1
            if longitude < intersection:
                inside = not inside
    return inside


def point_in_fir(longitude: float, latitude: float, feature: Mapping[str, Any]) -> bool:
    min_lon, min_lat, max_lon, max_lat = feature["bbox"]
    if not (min_lon <= longitude <= max_lon and min_lat <= latitude <= max_lat):
        return False
    for polygon in feature["polygons"]:
        if point_in_ring(longitude, latitude, polygon[0]) and not any(
            point_in_ring(longitude, latitude, hole) for hole in polygon[1:]
        ):
            return True
    return False


def find_fir(
    longitude: float,
    latitude: float,
    features: Sequence[Mapping[str, Any]],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    matches = [feature for feature in features if point_in_fir(longitude, latitude, feature)]
    if len(matches) == 1:
        return matches[0]["name"], matches[0]["designator"], None
    if not matches:
        return None, None, "no containing FIR polygon"
    designators = ", ".join(sorted(feature["designator"] for feature in matches))
    return None, None, f"ambiguous FIR polygon overlap ({designators})"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def valid_source_match(airport: Mapping[str, Any], row: Mapping[str, str]) -> Tuple[bool, str]:
    expected_country = COUNTRY_TO_ISO[airport["country"]]
    if str(row.get("iso_country", "")).strip().upper() != expected_country:
        return False, "country mismatch"
    source_latitude = parse_number(row.get("latitude_deg"))
    source_longitude = parse_number(row.get("longitude_deg"))
    if source_latitude is None or source_longitude is None:
        return False, "missing source coordinates"
    if not -90 <= source_latitude <= 90 or not -180 <= source_longitude <= 180:
        return False, "invalid source coordinates"
    distance = haversine_km(
        airport["latitude"], airport["longitude"], source_latitude, source_longitude
    )
    if distance > MAX_COORDINATE_DISTANCE_KM:
        return False, f"coordinate distance {distance:.1f} km exceeds limit"
    return True, ""


def build_indexes(rows: Sequence[Mapping[str, str]]) -> Tuple[Dict[str, List[Mapping[str, str]]], Dict[str, List[Mapping[str, str]]]]:
    exact: Dict[str, List[Mapping[str, str]]] = defaultdict(list)
    fallback: Dict[str, List[Mapping[str, str]]] = defaultdict(list)
    for row in rows:
        icao = str(row.get("icao_code", "")).strip().upper()
        if icao:
            exact[icao].append(row)
        for field in ("ident", "gps_code"):
            candidate = str(row.get(field, "")).strip().upper()
            if candidate and row not in fallback[candidate]:
                fallback[candidate].append(row)
    return exact, fallback


def select_source_row(
    airport: Mapping[str, Any],
    exact_index: Mapping[str, List[Mapping[str, str]]],
    fallback_index: Mapping[str, List[Mapping[str, str]]],
) -> Tuple[Optional[Mapping[str, str]], Optional[str], Optional[str]]:
    icao = airport["icao"]
    exact_candidates = exact_index.get(icao, [])
    valid_exact = [row for row in exact_candidates if valid_source_match(airport, row)[0]]
    if len(valid_exact) == 1:
        return valid_exact[0], "exact", None
    if len(valid_exact) > 1:
        return None, None, "ambiguous exact ICAO match"
    if exact_candidates:
        reasons = sorted({valid_source_match(airport, row)[1] for row in exact_candidates})
        return None, None, "exact ICAO rejected: " + ", ".join(reasons)

    fallback_candidates = fallback_index.get(icao, [])
    valid_fallback = [row for row in fallback_candidates if valid_source_match(airport, row)[0]]
    if len(valid_fallback) == 1:
        return valid_fallback[0], "fallback", None
    if len(valid_fallback) > 1:
        return None, None, "ambiguous controlled fallback"
    if fallback_candidates:
        reasons = sorted({valid_source_match(airport, row)[1] for row in fallback_candidates})
        return None, None, "fallback rejected: " + ", ".join(reasons)
    return None, None, "no OurAirports match"


STATIC_SOURCE_FIELDS = (
    "name",
    "type",
    "isoCountry",
    "countryName",
    "isoRegion",
    "regionName",
    "scheduledService",
    "gpsCode",
    "localCode",
    "ourAirportsId",
    "municipality",
    "iata",
    "elevationFt",
    "elevationM",
)
MAGNETIC_FIELDS = (
    "signedDeclinationDeg",
    "magneticVariationDeg",
    "magneticVariationDirection",
    "magneticVariationYear",
    "magneticVariationDate",
    "magneticVariationSource",
)


def optional_text(row: Mapping[str, str], field: str, *, upper: bool = False) -> Optional[str]:
    value = str(row.get(field, "")).strip()
    if not value:
        return None
    return value.upper() if upper else value


def optional_identifier(value: Any) -> Optional[int]:
    number = parse_number(value)
    if number is None or number <= 0 or not number.is_integer():
        return None
    return int(number)


def scheduled_service_value(value: Any) -> Optional[bool]:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if normalized == "yes":
        return True
    if normalized == "no":
        return False
    raise UpdateError(f"invalid OurAirports scheduled_service value {value!r}")


def source_metadata(
    row: Optional[Mapping[str, str]],
    countries: Optional[Mapping[str, Mapping[str, str]]] = None,
    regions: Optional[Mapping[str, Mapping[str, str]]] = None,
) -> Dict[str, Any]:
    if row is None:
        return {
            **{field: None for field in STATIC_SOURCE_FIELDS},
            "sourceCoordinates": {"latitude": None, "longitude": None},
        }
    countries = countries or {}
    regions = regions or {}
    municipality = str(row.get("municipality", "")).strip() or None
    iata = str(row.get("iata_code", "")).strip().upper() or None
    if iata is not None and not IATA_PATTERN.fullmatch(iata):
        iata = None
    elevation_ft = parse_number(row.get("elevation_ft"))
    elevation_ft = compact_number(elevation_ft)
    elevation_m = compact_number(float(elevation_ft) * 0.3048) if elevation_ft is not None else None
    iso_country = optional_text(row, "iso_country", upper=True)
    iso_region = optional_text(row, "iso_region", upper=True)
    source_latitude = compact_number(parse_number(row.get("latitude_deg")), 7)
    source_longitude = compact_number(parse_number(row.get("longitude_deg")), 7)
    return {
        "name": optional_text(row, "name"),
        "type": optional_text(row, "type"),
        "isoCountry": iso_country,
        "countryName": optional_text(countries.get(iso_country, {}), "name") if iso_country else None,
        "isoRegion": iso_region,
        "regionName": optional_text(regions.get(iso_region, {}), "name") if iso_region else None,
        "scheduledService": scheduled_service_value(row.get("scheduled_service")),
        "gpsCode": optional_text(row, "gps_code", upper=True),
        "localCode": optional_text(row, "local_code", upper=True),
        "ourAirportsId": optional_identifier(row.get("id")),
        "sourceCoordinates": {
            "latitude": source_latitude,
            "longitude": source_longitude,
        },
        "municipality": municipality,
        "iata": iata,
        "elevationFt": elevation_ft,
        "elevationM": elevation_m,
    }


def preserve_existing_source_values(
    metadata: Dict[str, Any], existing: Optional[Mapping[str, Any]]
) -> Dict[str, Any]:
    """Do not replace a previously valid OurAirports value with a transient null."""
    if not isinstance(existing, Mapping):
        return metadata
    for field in STATIC_SOURCE_FIELDS:
        if metadata.get(field) is None and existing.get(field) is not None:
            metadata[field] = existing[field]
    existing_coordinates = existing.get("sourceCoordinates")
    if isinstance(existing_coordinates, Mapping):
        for axis in ("latitude", "longitude"):
            if metadata["sourceCoordinates"].get(axis) is None and existing_coordinates.get(axis) is not None:
                metadata["sourceCoordinates"][axis] = existing_coordinates[axis]
    return metadata


def package_version(name: str) -> str:
    try:
        return package_metadata.version(name)
    except package_metadata.PackageNotFoundError:
        return "unknown"


def build_dataset(
    airports: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, str]],
    fir_features: Sequence[Mapping[str, Any]],
    retrieved_at: datetime,
    fir_retrieved_at: datetime,
    generated_at: datetime,
    countries: Optional[Mapping[str, Mapping[str, str]]] = None,
    regions: Optional[Mapping[str, Mapping[str, str]]] = None,
    existing_airports: Optional[Mapping[str, Mapping[str, Any]]] = None,
    countries_retrieved_at: Optional[datetime] = None,
    regions_retrieved_at: Optional[datetime] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    exact_index, fallback_index = build_indexes(rows)
    timezone_finder = TimezoneFinder(in_memory=True)
    output_airports: Dict[str, Any] = {}
    stats: Counter = Counter()
    warnings: List[str] = []

    for airport in sorted(airports, key=lambda item: item["icao"]):
        icao = airport["icao"]
        row, match_type, warning = select_source_row(airport, exact_index, fallback_index)
        if warning:
            warnings.append(f"{icao}: {warning}")
        if match_type:
            stats[f"{match_type}Matches"] += 1
        metadata = preserve_existing_source_values(
            source_metadata(row, countries, regions),
            (existing_airports or {}).get(icao),
        )
        for field in STATIC_SOURCE_FIELDS:
            if metadata[field] is not None:
                stats[field] += 1

        source_coordinates = metadata["sourceCoordinates"]
        magnetic_calculation_date = generated_at.date()
        try:
            signed_declination = calculate_signed_declination(
                source_coordinates["latitude"],
                source_coordinates["longitude"],
                magnetic_calculation_date,
                metadata["elevationM"],
            )
            magnetic = variation_fields(signed_declination, magnetic_calculation_date)
            stats["magneticVariations"] += 1
        except MagneticModelError as error:
            warnings.append(f"{icao}: magnetic variation unavailable ({error})")
            magnetic = {field: None for field in MAGNETIC_FIELDS}

        fir_name, fir_designator, fir_warning = find_fir(
            airport["longitude"], airport["latitude"], fir_features
        )
        if fir_warning:
            warnings.append(f"{icao}: FIR unavailable ({fir_warning})")
        else:
            stats["firs"] += 1

        timezone_name: Optional[str] = None
        try:
            candidate = timezone_finder.timezone_at(
                lng=airport["longitude"], lat=airport["latitude"]
            )
            if candidate:
                validated_zoneinfo(candidate)
                timezone_name = candidate
        except (ValueError, OverflowError) as error:
            warnings.append(f"{icao}: timezone unavailable ({error})")

        if timezone_name:
            stats["timezones"] += 1
            airport_day = local_date(generated_at, timezone_name)
            try:
                daylight = calculate_daylight(
                    airport["latitude"], airport["longitude"], timezone_name, airport_day
                )
            except (ArithmeticError, OverflowError, ValueError) as error:
                warnings.append(f"{icao}: daylight unavailable ({error})")
                daylight = {
                    "date": airport_day.isoformat(),
                    "sunriseUtc": None,
                    "solarNoonUtc": None,
                    "sunsetUtc": None,
                    "dayLengthSeconds": None,
                    "state": "unavailable",
                }
        else:
            daylight = {
                "date": None,
                "sunriseUtc": None,
                "solarNoonUtc": None,
                "sunsetUtc": None,
                "dayLengthSeconds": None,
                "state": "unavailable",
            }
        stats[f"daylight_{daylight['state']}"] += 1

        output_airports[icao] = {
            "icaoId": icao,
            **metadata,
            **magnetic,
            "fir": fir_name,
            "timezone": timezone_name,
            "daylight": daylight,
            "provenance": {
                "airportMetadata": "OurAirports" if row is not None else None,
                "countryName": "OurAirports countries.csv" if metadata["countryName"] else None,
                "regionName": "OurAirports regions.csv" if metadata["regionName"] else None,
                "sourceCoordinates": "OurAirports airports.csv" if row is not None else None,
                "magneticVariation": MAGNETIC_MODEL_NAME if magnetic["signedDeclinationDeg"] is not None else None,
                "timezone": "timezonefinder" if timezone_name else None,
                "fir": FIR_PROVIDER if fir_designator else None,
            },
        }

    document = {
        "schemaVersion": 1,
        "generatedAt": iso_utc(generated_at),
        "sources": {
            "airportMetadata": {
                "provider": "OurAirports",
                "url": OURAIRPORTS_URL,
                "retrievedAt": iso_utc(retrieved_at),
            },
            "countries": {
                "provider": "OurAirports",
                "url": OURAIRPORTS_COUNTRIES_URL,
                "retrievedAt": iso_utc(countries_retrieved_at or retrieved_at),
            },
            "regions": {
                "provider": "OurAirports",
                "url": OURAIRPORTS_REGIONS_URL,
                "retrievedAt": iso_utc(regions_retrieved_at or retrieved_at),
            },
            "magneticVariation": {
                "provider": MAGNETIC_MODEL_SOURCE,
                "model": MAGNETIC_MODEL_NAME,
                "calculationDate": generated_at.date().isoformat(),
                "signConvention": "east-positive, west-negative",
                "headingFormula": "magnetic = true - signed declination",
                "dependency": "pygeomag",
                "dependencyVersion": package_version("pygeomag"),
            },
            "firBoundaries": {
                "provider": FIR_PROVIDER,
                "originalUrl": FIR_ORIGINAL_URL,
                "mirrorUrl": FIR_BOUNDARIES_URL,
                "license": "CC BY 4.0",
                "europeanDesignatorsReference": FIR_EUROPE_REFERENCE_URL,
                "retrievedAt": iso_utc(fir_retrieved_at),
                "matchingMethod": "airport coordinate point-in-polygon",
            },
            "timezoneMapping": {
                "provider": "timezonefinder",
                "version": package_version("timezonefinder"),
            },
            "timezoneRules": {
                "provider": "IANA tzdb",
                "fallbackPackage": "tzdata",
                "fallbackVersion": package_version("tzdata"),
            },
            "daylight": {"method": "Astral", "version": package_version("astral")},
        },
        "airports": output_airports,
    }
    return document, {"counts": dict(stats), "warnings": warnings}


def validate_dataset(
    document: Mapping[str, Any],
    airports: Sequence[Mapping[str, Any]],
    *,
    require_static_fields: bool = True,
    require_magnetic_fields: bool = True,
) -> None:
    if document.get("schemaVersion") != 1:
        raise UpdateError("invalid schemaVersion")
    parse_utc(document.get("generatedAt"))
    output = document.get("airports")
    if not isinstance(output, dict):
        raise UpdateError("airports root must be an object")
    expected_ids = {airport["icao"] for airport in airports}
    if len(output) != EXPECTED_AIRPORT_COUNT or set(output) != expected_ids:
        raise UpdateError("output does not contain exactly the expected 618 unique ICAO airports")

    coordinates = {airport["icao"]: airport for airport in airports}
    allowed_states = {"normal", "polarDay", "polarNight", "unavailable"}
    for icao, record in output.items():
        if not ICAO_PATTERN.fullmatch(icao) or record.get("icaoId") != icao:
            raise UpdateError(f"invalid ICAO record {icao!r}")
        coordinate = coordinates[icao]
        if not -90 <= coordinate["latitude"] <= 90 or not -180 <= coordinate["longitude"] <= 180:
            raise UpdateError(f"invalid coordinates for {icao}")
        if require_static_fields:
            missing_fields = [field for field in STATIC_SOURCE_FIELDS if field not in record]
            if "sourceCoordinates" not in record:
                missing_fields.append("sourceCoordinates")
            if missing_fields:
                raise UpdateError(f"{icao} is missing static fields: {', '.join(missing_fields)}")
            for field in (
                "name", "type", "isoCountry", "countryName", "isoRegion", "regionName",
                "gpsCode", "localCode", "municipality", "iata",
            ):
                if record[field] is not None and (
                    not isinstance(record[field], str) or not record[field].strip()
                ):
                    raise UpdateError(f"invalid {field} for {icao}")
            if record["isoCountry"] is not None and not COUNTRY_CODE_PATTERN.fullmatch(record["isoCountry"]):
                raise UpdateError(f"invalid isoCountry for {icao}")
            if record["isoRegion"] is not None and not REGION_CODE_PATTERN.fullmatch(record["isoRegion"]):
                raise UpdateError(f"invalid isoRegion for {icao}")
            if record["scheduledService"] is not None and not isinstance(record["scheduledService"], bool):
                raise UpdateError(f"invalid scheduledService for {icao}")
            if record["ourAirportsId"] is not None and (
                not isinstance(record["ourAirportsId"], int) or record["ourAirportsId"] <= 0
            ):
                raise UpdateError(f"invalid ourAirportsId for {icao}")
            source_coordinates = record["sourceCoordinates"]
            if not isinstance(source_coordinates, dict):
                raise UpdateError(f"invalid sourceCoordinates for {icao}")
            source_latitude = source_coordinates.get("latitude")
            source_longitude = source_coordinates.get("longitude")
            if (source_latitude is None) != (source_longitude is None):
                raise UpdateError(f"incomplete sourceCoordinates for {icao}")
            if source_latitude is not None and (
                not isinstance(source_latitude, (int, float))
                or isinstance(source_latitude, bool)
                or not -90 <= source_latitude <= 90
                or not isinstance(source_longitude, (int, float))
                or isinstance(source_longitude, bool)
                or not -180 <= source_longitude <= 180
            ):
                raise UpdateError(f"out-of-range sourceCoordinates for {icao}")
        if require_magnetic_fields:
            missing_magnetic = [field for field in MAGNETIC_FIELDS if field not in record]
            if missing_magnetic:
                raise UpdateError(f"{icao} is missing magnetic fields: {', '.join(missing_magnetic)}")
            signed = record["signedDeclinationDeg"]
            magnitude = record["magneticVariationDeg"]
            direction = record["magneticVariationDirection"]
            year = record["magneticVariationYear"]
            calculation_date = record["magneticVariationDate"]
            source = record["magneticVariationSource"]
            dependent_values = (magnitude, year, calculation_date, source)
            if signed is None:
                if any(value is not None for value in dependent_values) or direction is not None:
                    raise UpdateError(f"incomplete magnetic variation for {icao}")
            else:
                if any(value is None for value in dependent_values):
                    raise UpdateError(f"incomplete magnetic variation for {icao}")
                if (
                    not isinstance(signed, (int, float))
                    or isinstance(signed, bool)
                    or not math.isfinite(signed)
                    or not isinstance(magnitude, (int, float))
                    or isinstance(magnitude, bool)
                    or magnitude < 0
                    or magnitude != round(abs(signed), 1)
                ):
                    raise UpdateError(f"invalid magnetic variation values for {icao}")
                expected_direction = "E" if signed > 0 else "W" if signed < 0 else None
                if direction != expected_direction:
                    raise UpdateError(f"invalid magnetic variation direction for {icao}")
                parsed_date = datetime.strptime(calculation_date, "%Y-%m-%d").date()
                if not isinstance(year, int) or isinstance(year, bool) or year != parsed_date.year:
                    raise UpdateError(f"invalid magnetic variation year for {icao}")
                if source != MAGNETIC_MODEL_NAME:
                    raise UpdateError(f"invalid magnetic variation source for {icao}")
        timezone_name = record.get("timezone")
        if timezone_name is not None:
            validated_zoneinfo(timezone_name)
        fir_name = record.get("fir")
        if fir_name is not None and (
            not isinstance(fir_name, str) or not fir_name.strip() or "FIR" not in fir_name.upper()
        ):
            raise UpdateError(f"invalid FIR value for {icao}")
        daylight = record.get("daylight")
        if not isinstance(daylight, dict) or daylight.get("state") not in allowed_states:
            raise UpdateError(f"invalid daylight record for {icao}")
        if daylight.get("date") is not None:
            datetime.strptime(daylight["date"], "%Y-%m-%d")
        day_length = daylight.get("dayLengthSeconds")
        if day_length is not None and (not isinstance(day_length, int) or day_length < 0):
            raise UpdateError(f"invalid day length for {icao}")
        for field in ("sunriseUtc", "solarNoonUtc", "sunsetUtc"):
            if daylight.get(field) is not None:
                parse_utc(daylight[field])
        if daylight["state"] == "normal":
            if any(daylight.get(field) is None for field in ("sunriseUtc", "solarNoonUtc", "sunsetUtc")):
                raise UpdateError(f"normal daylight record for {icao} has missing events")
            rise = parse_utc(daylight["sunriseUtc"])
            noon_ = parse_utc(daylight["solarNoonUtc"])
            set_ = parse_utc(daylight["sunsetUtc"])
            if not rise < noon_ < set_:
                raise UpdateError(f"illogical daylight event order for {icao}")
            if int((set_ - rise).total_seconds()) != day_length:
                raise UpdateError(f"day length does not match events for {icao}")


def load_existing_airport_metadata(
    path: Path, airports: Sequence[Mapping[str, Any]]
) -> Dict[str, Mapping[str, Any]]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            document = json.load(handle)
        # Legacy schemaVersion 1 documents are accepted here so the first
        # schema extension can safely preserve their already valid values.
        validate_dataset(
            document,
            airports,
            require_static_fields=False,
            require_magnetic_fields=False,
        )
    except (OSError, json.JSONDecodeError, UpdateError, ValueError) as error:
        raise UpdateError(f"existing output {path} is invalid; refusing to overwrite it: {error}") from error
    return document["airports"]


def load_existing_metadata_document(
    path: Path, airports: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    """Load a fully valid current document for a daylight-only refresh."""
    if not path.exists():
        raise UpdateError(f"daylight-only update requires an existing {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        validate_dataset(document, airports)
    except (OSError, json.JSONDecodeError, UpdateError, ValueError) as error:
        raise UpdateError(
            f"existing output {path} is invalid; daylight-only update aborted: {error}"
        ) from error
    return document


def build_daylight_only_document(
    existing: Mapping[str, Any],
    airports: Sequence[Mapping[str, Any]],
    generated_at: datetime,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Refresh only daylight snapshots from stored coordinates/timezones."""
    document = copy.deepcopy(existing)
    records = document.get("airports")
    if not isinstance(records, dict):
        raise UpdateError("existing metadata has no airports object")
    counts: Counter = Counter()
    warnings: List[str] = []
    for airport in airports:
        icao = airport["icao"]
        record = records.get(icao)
        if not isinstance(record, dict):
            raise UpdateError(f"existing metadata record {icao} is missing")
        coordinates = record.get("sourceCoordinates")
        timezone_name = record.get("timezone")
        if not isinstance(coordinates, dict):
            raise UpdateError(f"{icao} has no stored source coordinates")
        latitude = parse_number(coordinates.get("latitude"))
        longitude = parse_number(coordinates.get("longitude"))
        if latitude is None or longitude is None or timezone_name is None:
            raise UpdateError(f"{icao} lacks coordinates/timezone required for daylight refresh")
        try:
            validated_zoneinfo(timezone_name)
            airport_day = local_date(generated_at, timezone_name)
            daylight = calculate_daylight(latitude, longitude, timezone_name, airport_day)
        except (ArithmeticError, OverflowError, TypeError, ValueError) as error:
            warnings.append(f"{icao}: daylight unavailable ({error})")
            daylight = {
                "date": None,
                "sunriseUtc": None,
                "solarNoonUtc": None,
                "sunsetUtc": None,
                "dayLengthSeconds": None,
                "state": "unavailable",
            }
        record["daylight"] = daylight
        counts[f"daylight_{daylight['state']}"] += 1

    document["generatedAt"] = iso_utc(generated_at)
    sources = document.get("sources")
    if not isinstance(sources, dict) or not isinstance(sources.get("daylight"), dict):
        raise UpdateError("existing metadata has no daylight provenance")
    sources["daylight"]["refreshedAt"] = iso_utc(generated_at)
    return document, {"counts": dict(counts), "warnings": warnings}


def atomic_write_json(path: Path, document: Mapping[str, Any]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary_name = handle.name
            json.dump(document, handle, ensure_ascii=False, indent=2)
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


def parse_now(value: Optional[str]) -> datetime:
    if value is None:
        return utc_now()
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise UpdateError(f"invalid --now timestamp: {value!r}") from error
    if moment.tzinfo is None:
        raise UpdateError("--now must include a UTC offset")
    return moment.astimezone(timezone.utc)


def print_report(stats: Mapping[str, Any], size: int, generated_at: str) -> None:
    counts = Counter(stats["counts"])
    warnings = stats["warnings"]
    print(f"processedAirportCount: {EXPECTED_AIRPORT_COUNT}")
    print(f"exactOurAirportsMatches: {counts['exactMatches']}")
    print(f"controlledFallbackMatches: {counts['fallbackMatches']}")
    print(f"airportsWithMunicipality: {counts['municipality']}")
    print(f"airportsWithIata: {counts['iata']}")
    print(f"airportsWithElevation: {counts['elevationFt']}")
    print(f"airportsWithMagneticVariation: {counts['magneticVariations']}")
    print(f"airportsWithoutMagneticVariation: {EXPECTED_AIRPORT_COUNT - counts['magneticVariations']}")
    for field in (
        "name", "type", "isoCountry", "countryName", "isoRegion", "regionName",
        "scheduledService", "gpsCode", "localCode", "ourAirportsId",
    ):
        print(f"airportsWith{field[0].upper() + field[1:]}: {counts[field]}")
    print(f"airportsWithValidTimezone: {counts['timezones']}")
    print(f"airportsWithoutTimezone: {EXPECTED_AIRPORT_COUNT - counts['timezones']}")
    print(f"airportsWithFir: {counts['firs']}")
    print(f"airportsWithoutFir: {EXPECTED_AIRPORT_COUNT - counts['firs']}")
    print(f"daylightNormal: {counts['daylight_normal']}")
    print(f"daylightPolarDay: {counts['daylight_polarDay']}")
    print(f"daylightPolarNight: {counts['daylight_polarNight']}")
    print(f"daylightUnavailable: {counts['daylight_unavailable']}")
    print(f"warningCount: {len(warnings)}")
    for warning in warnings:
        print(f"warning: {warning}")
    print(f"outputBytes: {size}")
    print(f"generatedAt: {generated_at}")


def print_daylight_report(stats: Mapping[str, Any], size: int, generated_at: str) -> None:
    counts = Counter(stats["counts"])
    warnings = stats["warnings"]
    print("mode: daylight-only")
    print(f"processedAirportCount: {EXPECTED_AIRPORT_COUNT}")
    print(f"daylightNormal: {counts['daylight_normal']}")
    print(f"daylightPolarDay: {counts['daylight_polarDay']}")
    print(f"daylightPolarNight: {counts['daylight_polarNight']}")
    print(f"daylightUnavailable: {counts['daylight_unavailable']}")
    print(f"warningCount: {len(warnings)}")
    for warning in warnings:
        print(f"warning: {warning}")
    print(f"outputBytes: {size}")
    print(f"generatedAt: {generated_at}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-file", type=Path, help="use a local OurAirports airports.csv fixture")
    parser.add_argument("--countries-file", type=Path, help="use a local OurAirports countries.csv fixture")
    parser.add_argument("--regions-file", type=Path, help="use a local OurAirports regions.csv fixture")
    parser.add_argument("--fir-source-file", type=Path, help="use a local FIR GeoJSON fixture")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH, help="output JSON path")
    parser.add_argument("--now", help="override current UTC time for deterministic testing")
    parser.add_argument(
        "--daylight-only",
        action="store_true",
        help="refresh daylight from stored coordinates/timezones without downloading static sources",
    )
    args = parser.parse_args(argv)

    try:
        airports = load_airports()
        output_path = args.output.resolve()
        generated_at = parse_now(args.now)
        if args.daylight_only:
            existing_document = load_existing_metadata_document(output_path, airports)
            document, stats = build_daylight_only_document(
                existing_document, airports, generated_at
            )
            validate_dataset(document, airports)
            size = atomic_write_json(output_path, document)
            print_daylight_report(stats, size, document["generatedAt"])
            return 0
        existing_airports = load_existing_airport_metadata(output_path, airports)
        # FIR source validation is intentionally completed before any output is
        # built. A missing/corrupt source therefore cannot replace valid data.
        if args.fir_source_file:
            fir_text, fir_retrieved_at = read_fir_source(args.fir_source_file)
        else:
            fir_text, fir_retrieved_at = download_fir_geojson()
        fir_features = parse_fir_boundaries(fir_text)
        if args.source_file:
            csv_text, retrieved_at = read_source_csv(args.source_file, "airports.csv")
        else:
            csv_text, retrieved_at = download_airports_csv()
        rows = parse_ourairports(csv_text)
        if args.countries_file:
            countries_text, countries_retrieved_at = read_source_csv(
                args.countries_file, "countries.csv"
            )
        else:
            countries_text, countries_retrieved_at = download_countries_csv()
        countries = parse_countries(countries_text)
        if len(countries) < 200:
            raise UpdateError(f"OurAirports countries.csv unexpectedly contains only {len(countries)} rows")
        if args.regions_file:
            regions_text, regions_retrieved_at = read_source_csv(args.regions_file, "regions.csv")
        else:
            regions_text, regions_retrieved_at = download_regions_csv()
        regions = parse_regions(regions_text)
        if len(regions) < 1000:
            raise UpdateError(f"OurAirports regions.csv unexpectedly contains only {len(regions)} rows")
        document, stats = build_dataset(
            airports,
            rows,
            fir_features,
            retrieved_at,
            fir_retrieved_at,
            generated_at,
            countries,
            regions,
            existing_airports,
            countries_retrieved_at,
            regions_retrieved_at,
        )
        validate_dataset(document, airports)
        size = atomic_write_json(output_path, document)
        print_report(stats, size, document["generatedAt"])
        return 0
    except (UpdateError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
