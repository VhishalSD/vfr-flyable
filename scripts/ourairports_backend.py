#!/usr/bin/env python3
"""Shared safe-download and atomic-publication helpers for OurAirports data."""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple


REQUEST_TIMEOUT_SECONDS = 90
MAX_ATTEMPTS = 3
USER_AGENT = "vfr-flyable-airport-geodata-updater/1.0 (+local-development)"
ICAO_PATTERN = re.compile(r"^[A-Z]{4}$")


class UpdateError(RuntimeError):
    """Raised when no complete, safely publishable dataset can be produced."""


def optional_number(value: Any, *, integer: bool = False) -> Optional[float | int]:
    if value is None or isinstance(value, bool) or not str(value).strip():
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError) as error:
        raise UpdateError(f"invalid numeric source value {value!r}") from error
    if not math.isfinite(number):
        raise UpdateError(f"non-finite numeric source value {value!r}")
    if integer:
        if not number.is_integer():
            raise UpdateError(f"expected integer source value, got {value!r}")
        return int(number)
    return int(number) if number.is_integer() else number


def optional_text(value: Any, *, upper: bool = False) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    return text.upper() if upper else text


def load_origins(path: Path, expected_count: int = 618) -> dict[str, dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UpdateError(f"cannot read valid airport metadata from {path}: {error}") from error
    records = document.get("airports") if isinstance(document, dict) else None
    if not isinstance(records, dict) or len(records) != expected_count:
        raise UpdateError(f"airport metadata must contain exactly {expected_count} airports")
    origins: dict[str, dict[str, Any]] = {}
    for icao, record in records.items():
        normalized_icao = str(icao).strip().upper()
        if normalized_icao != icao or not ICAO_PATTERN.fullmatch(normalized_icao) or not isinstance(record, dict):
            raise UpdateError(f"invalid airport metadata record {icao!r}")
        coordinates = record.get("sourceCoordinates")
        if not isinstance(coordinates, dict):
            raise UpdateError(f"{icao} has no sourceCoordinates")
        latitude = optional_number(coordinates.get("latitude"))
        longitude = optional_number(coordinates.get("longitude"))
        source_id = optional_number(record.get("ourAirportsId"), integer=True)
        if (
            latitude is None
            or longitude is None
            or source_id is None
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
        ):
            raise UpdateError(f"{icao} has invalid coordinates or OurAirports id")
        origins[icao] = {
            "icaoId": icao,
            "ourAirportsId": source_id,
            "latitude": latitude,
            "longitude": longitude,
        }
    return origins


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def download_csv(
    url: str,
    label: str,
    required_columns: Sequence[str],
) -> Tuple[str, datetime, list[str]]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/csv"})
    errors: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                if response.status != 200:
                    raise UpdateError(f"OurAirports {label} returned HTTP {response.status}")
                payload = response.read()
            text = payload.decode("utf-8-sig")
            header = next(csv.reader(io.StringIO(text)), [])
            if not set(required_columns).issubset(set(header)):
                raise UpdateError(f"response does not look like {label}")
            return text, utc_now(), errors
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError, UpdateError) as error:
            errors.append(f"attempt {attempt}: {error}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(attempt * 2)
    raise UpdateError(f"failed to download OurAirports {label}; " + "; ".join(errors))


def read_csv_fixture(path: Path, label: str) -> Tuple[str, datetime]:
    try:
        return path.read_text(encoding="utf-8-sig"), utc_now()
    except OSError as error:
        raise UpdateError(f"cannot read {label} fixture {path}: {error}") from error


def atomic_write_json(path: Path, document: Mapping[str, Any]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(document, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        descriptor = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    return path.stat().st_size
