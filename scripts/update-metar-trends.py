#!/usr/bin/env python3
"""Generate compact monthly METAR trends from the permanent local archive."""

from __future__ import annotations

import argparse
import calendar
import importlib.util
import json
import math
import os
import shutil
import tempfile
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RECENT_UPDATER_PATH = PROJECT_ROOT / "scripts" / "update-metar-recent.py"
ARCHIVE_UPDATER_PATH = PROJECT_ROOT / "scripts" / "update-metar-archive.py"
ARCHIVE_DIR = PROJECT_ROOT / "data" / "metar-archive"
METADATA_PATH = PROJECT_ROOT / "data" / "airport-metadata.json"
OUTPUT_DIR = PROJECT_ROOT / "data" / "metar-trends"
SCHEMA_VERSION = 1
MIN_COMPLETE_DAY_OBSERVATIONS = 12
MIN_COMPLETE_DAY_SPAN_HOURS = 18
MIN_COMPLETE_YEARS_FOR_AVERAGE = 2
KM_PER_STATUTE_MILE = 1.609344
VFR_VISIBILITY_SM = 8.0 / KM_PER_STATUTE_MILE
MVFR_VISIBILITY_SM = 5.0 / KM_PER_STATUTE_MILE
EXPLICIT_NO_CEILING_COVERS = {"CAVOK", "CLR", "SKC", "NSC", "NCD"}
NON_CEILING_COVERS = EXPLICIT_NO_CEILING_COVERS | {"FEW", "SCT"}


class TrendsError(RuntimeError):
    """Raised when trends cannot be generated without risking valid output."""


def load_script(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise TrendsError(f"Cannot load shared backend module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recent = load_script("metar_recent_for_trends", RECENT_UPDATER_PATH)
archive = load_script("metar_archive_for_trends", ARCHIVE_UPDATER_PATH)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return recent.iso_utc(value)


def finite_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def rounded(value: Optional[float], digits: int = 1) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    result = round(value, digits)
    return int(result) if result == int(result) else result


def mean(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def relative_humidity_percent(temperature_c: Any, dew_point_c: Any) -> Optional[float]:
    temperature = finite_number(temperature_c)
    dew_point = finite_number(dew_point_c)
    if temperature is None or dew_point is None:
        return None
    denominator_t = 243.04 + temperature
    denominator_td = 243.04 + dew_point
    if denominator_t == 0 or denominator_td == 0:
        return None
    result = 100.0 * math.exp(
        17.625 * dew_point / denominator_td
        - 17.625 * temperature / denominator_t
    )
    # Supersaturation can occur through METAR whole-degree rounding. Keep the
    # published percentage physically bounded without inventing observations.
    return min(100.0, max(0.0, result))


def observation_dew_point(record: Mapping[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    normalized = finite_number(record.get("dewPointC"))
    if normalized is not None:
        return normalized, str(record.get("dewPointSource") or "normalized")
    parsed = recent.parse_dew_point_from_raw(record.get("rawOb"))
    return (float(parsed), "rawObFallback") if parsed is not None else (None, None)


def visibility_meets(record: Mapping[str, Any], threshold_sm: float) -> Optional[bool]:
    visibility = finite_number(record.get("visib"))
    if visibility is None or visibility < 0:
        return None
    qualifier = record.get("visibilityQualifier")
    if qualifier in {None, "exact"}:
        return visibility >= threshold_sm
    if qualifier == "greaterThan":
        return True if visibility >= threshold_sm else None
    if qualifier == "lessThan":
        return False if visibility <= threshold_sm else None
    return None


def ceiling_value(record: Mapping[str, Any]) -> Tuple[Optional[float], bool]:
    """Return (ceiling feet, known); infinity represents explicit no ceiling."""
    ceiling = finite_number(record.get("ceilingFt"))
    if ceiling is not None and ceiling >= 0:
        return ceiling, True
    layers = record.get("cloudLayers")
    if not isinstance(layers, list):
        return None, False
    covers = {
        str(layer.get("cover", "")).upper()
        for layer in layers
        if isinstance(layer, dict) and layer.get("cover")
    }
    if covers and covers <= NON_CEILING_COVERS:
        return math.inf, True
    raw = str(record.get("rawOb") or "").upper().split()
    if any(token in EXPLICIT_NO_CEILING_COVERS for token in raw):
        return math.inf, True
    return None, False


def condition_result(
    record: Mapping[str, Any], visibility_threshold_sm: float, ceiling_threshold_ft: float
) -> Optional[bool]:
    visibility = visibility_meets(record, visibility_threshold_sm)
    ceiling, ceiling_known = ceiling_value(record)
    if visibility is None or not ceiling_known or ceiling is None:
        return None
    return visibility and ceiling >= ceiling_threshold_ft


def is_complete_local_day(records: Sequence[Tuple[datetime, Mapping[str, Any]]]) -> bool:
    if len(records) < MIN_COMPLETE_DAY_OBSERVATIONS:
        return False
    moments = sorted(item[0] for item in records)
    return (moments[-1] - moments[0]).total_seconds() >= MIN_COMPLETE_DAY_SPAN_HOURS * 3600


def local_month_is_partial(
    year: int,
    month: int,
    complete_dates: set[date],
    generated_at: datetime,
    zone: ZoneInfo,
) -> bool:
    local_now = generated_at.astimezone(zone)
    if (year, month) >= (local_now.year, local_now.month):
        return True
    days = calendar.monthrange(year, month)[1]
    expected = {date(year, month, day) for day in range(1, days + 1)}
    return not expected.issubset(complete_dates)


def month_metrics(
    entries: Sequence[Tuple[datetime, Mapping[str, Any]]],
    zone: ZoneInfo,
    generated_at: datetime,
) -> Dict[str, Any]:
    by_day: Dict[date, list[Tuple[datetime, Mapping[str, Any]]]] = defaultdict(list)
    for moment, record in entries:
        by_day[moment.astimezone(zone).date()].append((moment, record))
    complete_dates = {day for day, values in by_day.items() if is_complete_local_day(values)}

    vfr_results = [condition_result(record, VFR_VISIBILITY_SM, 3000) for _, record in entries]
    mvfr_results = [condition_result(record, MVFR_VISIBILITY_SM, 1000) for _, record in entries]
    eligible_vfr = [value for value in vfr_results if value is not None]
    eligible_mvfr = [value for value in mvfr_results if value is not None]

    daily_maxima: list[float] = []
    daily_minima: list[float] = []
    daily_humidity: list[float] = []
    daily_wind: list[float] = []
    humidity_observations = 0
    wind_observations = 0
    temperature_observations = 0
    dew_point_normalized = 0
    dew_point_fallback = 0
    for day in sorted(complete_dates):
        temperatures: list[float] = []
        humidities: list[float] = []
        winds: list[float] = []
        for _moment, record in by_day[day]:
            temperature = finite_number(record.get("temp"))
            if temperature is not None:
                temperatures.append(temperature)
                temperature_observations += 1
            dew_point, source = observation_dew_point(record)
            humidity = relative_humidity_percent(temperature, dew_point)
            if humidity is not None:
                humidities.append(humidity)
                humidity_observations += 1
                if source == "rawObFallback":
                    dew_point_fallback += 1
                else:
                    dew_point_normalized += 1
            wind = finite_number(record.get("windSpeed"))
            if wind is not None and wind >= 0:
                winds.append(wind)
                wind_observations += 1
        if temperatures:
            daily_maxima.append(max(temperatures))
            daily_minima.append(min(temperatures))
        if humidities:
            daily_humidity.append(mean(humidities))  # type: ignore[arg-type]
        if winds:
            daily_wind.append(mean(winds))  # type: ignore[arg-type]

    first = min(item[0] for item in entries)
    last = max(item[0] for item in entries)
    year, month = first.astimezone(zone).year, first.astimezone(zone).month
    return {
        "partialMonth": local_month_is_partial(year, month, complete_dates, generated_at, zone),
        "vfrPercent": rounded(100 * sum(bool(value) for value in eligible_vfr) / len(eligible_vfr)) if eligible_vfr else None,
        "mvfrOrBetterPercent": rounded(100 * sum(bool(value) for value in eligible_mvfr) / len(eligible_mvfr)) if eligible_mvfr else None,
        "maxTemperatureC": rounded(mean(daily_maxima)),
        "minTemperatureC": rounded(mean(daily_minima)),
        "relativeHumidityPercent": rounded(mean(daily_humidity)),
        "windSpeedKt": rounded(mean(daily_wind)),
        "quality": {
            "observationCount": len(entries),
            "eligibleObservationCount": {
                "vfr": len(eligible_vfr),
                "mvfrOrBetter": len(eligible_mvfr),
                "temperature": temperature_observations,
                "relativeHumidity": humidity_observations,
                "windSpeed": wind_observations,
            },
            "availableLocalDays": len(by_day),
            "completeLocalDays": len(complete_dates),
            "temperatureDailyValueCount": len(daily_maxima),
            "relativeHumidityDailyValueCount": len(daily_humidity),
            "windSpeedDailyValueCount": len(daily_wind),
            "dewPointNormalizedCount": dew_point_normalized,
            "dewPointRawFallbackCount": dew_point_fallback,
            "firstObservation": iso_utc(first),
            "lastObservation": iso_utc(last),
        },
    }


DEFINITIONS = {
    "vfrPercent": {
        "definition": "percentage of eligible observations with visibility >= 8 km and ceiling >= 3000 ft",
        "weighting": "observation-based",
    },
    "mvfrOrBetterPercent": {
        "definition": "percentage of eligible observations with visibility >= 5 km and ceiling >= 1000 ft",
        "weighting": "observation-based",
    },
    "maxTemperatureC": {"definition": "monthly mean of complete-local-day maxima", "weighting": "each complete local day equally"},
    "minTemperatureC": {"definition": "monthly mean of complete-local-day minima", "weighting": "each complete local day equally"},
    "relativeHumidityPercent": {"definition": "Magnus RH from temperature/dew point, averaged per complete local day then month", "weighting": "each complete local day equally"},
    "windSpeedKt": {"definition": "sustained wind averaged per complete local day then month; gusts excluded", "weighting": "each complete local day equally"},
    "eligibility": {
        "ceiling": "numeric BKN/OVC/OVX or vertical visibility; explicit FEW/SCT/CLR/SKC/NSC/NCD/CAVOK means unlimited; unknown cloud state excluded",
        "visibility": "exact values are compared directly; bounded qualifiers are used only when the threshold outcome is unambiguous",
    },
    "completeLocalDay": {"minimumObservations": MIN_COMPLETE_DAY_OBSERVATIONS, "minimumSpanHours": MIN_COMPLETE_DAY_SPAN_HOURS},
    "climatologicalAverage": {"minimumCompleteYearsPerCalendarMonth": MIN_COMPLETE_YEARS_FOR_AVERAGE},
}


def build_averages(years: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> Dict[str, Any]:
    averages: Dict[str, Any] = {}
    metric_names = ("vfrPercent", "mvfrOrBetterPercent", "maxTemperatureC", "minTemperatureC", "relativeHumidityPercent", "windSpeedKt")
    for month in range(1, 13):
        complete = [months[str(month)] for months in years.values() if str(month) in months and months[str(month)].get("partialMonth") is False]
        if len(complete) < MIN_COMPLETE_YEARS_FOR_AVERAGE:
            continue
        averages[str(month)] = {
            "completeYearCount": len(complete),
            **{name: rounded(mean([float(item[name]) for item in complete if finite_number(item.get(name)) is not None])) for name in metric_names},
        }
    return averages


def aggregate_station(
    icao: str,
    timezone_name: str,
    observations: Iterable[Mapping[str, Any]],
    generated_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    generated = generated_at or utc_now()
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    generated = generated.astimezone(timezone.utc)
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise TrendsError(f"Invalid timezone for {icao}: {timezone_name}") from error
    grouped: Dict[Tuple[int, int], list[Tuple[datetime, Mapping[str, Any]]]] = defaultdict(list)
    seen: set[str] = set()
    for record in observations:
        if not isinstance(record, Mapping):
            raise TrendsError(f"Invalid observation object for {icao}")
        moment = recent.parse_timestamp(record.get("obsTime"))
        if moment is None:
            raise TrendsError(f"Invalid obsTime for {icao}")
        canonical = iso_utc(moment)
        if canonical in seen:
            raise TrendsError(f"Duplicate obsTime for {icao}: {canonical}")
        seen.add(canonical)
        local = moment.astimezone(zone)
        grouped[(local.year, local.month)].append((moment, record))
    if not grouped:
        raise TrendsError(f"No archive observations for {icao}")
    years: Dict[str, Dict[str, Any]] = defaultdict(dict)
    all_moments = [moment for entries in grouped.values() for moment, _ in entries]
    for (year, month), entries in sorted(grouped.items()):
        years[str(year)][str(month)] = month_metrics(entries, zone, generated)
    document = {
        "schemaVersion": SCHEMA_VERSION,
        "icao": icao,
        "generatedAt": iso_utc(generated),
        "timezone": timezone_name,
        "source": {"dataset": "data/metar-archive", "canonicalTime": "obsTime UTC", "calendarGrouping": "airport local time"},
        "coverage": {"firstObservation": iso_utc(min(all_moments)), "lastObservation": iso_utc(max(all_moments))},
        "definitions": DEFINITIONS,
        "averages": build_averages(years),
        "years": dict(years),
    }
    valid, detail = validate_document(document, icao)
    if not valid:
        raise TrendsError(f"Generated trends failed validation: {detail}")
    return document


def validate_document(document: Any, expected_icao: Optional[str] = None) -> Tuple[bool, str]:
    if not isinstance(document, dict) or document.get("schemaVersion") != SCHEMA_VERSION:
        return False, "invalid root/schemaVersion"
    icao = document.get("icao")
    if not isinstance(icao, str) or not recent.ICAO_PATTERN.fullmatch(icao) or (expected_icao and icao != expected_icao):
        return False, "invalid ICAO"
    try:
        ZoneInfo(document.get("timezone"))
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return False, "invalid timezone"
    if recent.parse_timestamp(document.get("generatedAt")) is None:
        return False, "invalid generatedAt"
    coverage = document.get("coverage")
    if not isinstance(coverage, dict) or recent.parse_timestamp(coverage.get("firstObservation")) is None or recent.parse_timestamp(coverage.get("lastObservation")) is None:
        return False, "invalid coverage"
    years = document.get("years")
    if not isinstance(years, dict) or not years:
        return False, "missing years"
    for year, months in years.items():
        if not str(year).isdigit() or not isinstance(months, dict):
            return False, "invalid year"
        for month, values in months.items():
            if not str(month).isdigit() or not 1 <= int(month) <= 12 or not isinstance(values, dict):
                return False, "invalid month"
            for field in ("vfrPercent", "mvfrOrBetterPercent", "maxTemperatureC", "minTemperatureC", "relativeHumidityPercent", "windSpeedKt"):
                value = values.get(field)
                if value is not None and finite_number(value) is None:
                    return False, f"invalid/non-finite {field}"
            if not isinstance(values.get("partialMonth"), bool) or not isinstance(values.get("quality"), dict):
                return False, "invalid month quality"
    return True, f"{icao}: {sum(len(months) for months in years.values())} months"


def load_metadata(path: Path = METADATA_PATH) -> Dict[str, str]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TrendsError(f"Cannot read airport metadata: {error}") from error
    airports = document.get("airports") if isinstance(document, dict) else None
    if not isinstance(airports, dict):
        raise TrendsError("airport-metadata.json has no airports object")
    return {icao: record.get("timezone") for icao, record in airports.items() if isinstance(record, dict) and isinstance(record.get("timezone"), str)}


def load_station_observations(icao: str, root: Path = ARCHIVE_DIR) -> list[Dict[str, Any]]:
    observations: list[Dict[str, Any]] = []
    paths = sorted((root / icao).glob("[0-9][0-9][0-9][0-9]/[0-9][0-9].json"))
    if not paths:
        raise TrendsError(f"No archive partitions for {icao}")
    for path in paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise TrendsError(f"Corrupt archive partition {path}: {error}") from error
        valid, detail = archive.validate_partition(document, icao, int(path.parent.name), int(path.stem))
        if not valid:
            raise TrendsError(f"Invalid archive partition {path}: {detail}")
        observations.extend(document["observations"])
    return observations


def write_fsynced(path: Path, document: Mapping[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_file(path: Path, document: Mapping[str, Any], replace: Any = os.replace) -> None:
    valid, detail = validate_document(document, path.stem.upper())
    if not valid:
        raise TrendsError(f"Refusing invalid output: {detail}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temp = Path(temp_name)
    try:
        write_fsynced(temp, document)
        replace(temp, path)
        fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def publish_all(documents: Mapping[str, Mapping[str, Any]], output_dir: Path = OUTPUT_DIR) -> None:
    parent = output_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".metar-trends-stage-", dir=parent))
    backup = parent / f".metar-trends-backup-{uuid.uuid4().hex}"
    old_moved = False
    new_moved = False
    try:
        for icao, document in sorted(documents.items()):
            valid, detail = validate_document(document, icao)
            if not valid:
                raise TrendsError(f"Refusing invalid {icao}: {detail}")
            write_fsynced(stage / f"{icao}.json", document)
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


def generate(selected_icao: Optional[str] = None, generated_at: Optional[datetime] = None) -> Dict[str, Any]:
    metadata = load_metadata()
    index_path = ARCHIVE_DIR / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TrendsError(f"Cannot read archive index: {error}") from error
    stations = index.get("stations") if isinstance(index, dict) else None
    if not isinstance(stations, dict) or not stations:
        raise TrendsError("Archive index has no stations")
    ids = [selected_icao.upper()] if selected_icao else sorted(stations)
    if selected_icao and selected_icao.upper() not in stations:
        raise TrendsError(f"ICAO not in archive: {selected_icao.upper()}")
    documents: Dict[str, Dict[str, Any]] = {}
    warnings: list[str] = []
    for icao in ids:
        timezone_name = metadata.get(icao)
        if not timezone_name:
            warnings.append(f"{icao}: missing timezone")
            continue
        try:
            documents[icao] = aggregate_station(icao, timezone_name, load_station_observations(icao), generated_at)
        except TrendsError as error:
            warnings.append(str(error))
    if not documents:
        raise TrendsError("No valid trends documents generated")
    if selected_icao:
        publish_file(OUTPUT_DIR / f"{selected_icao.upper()}.json", documents[selected_icao.upper()])
    else:
        publish_all(documents)
    return {"requestedStations": len(ids), "generatedStations": len(documents), "warnings": warnings}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--icao", help="Generate one archived ICAO station")
    args = parser.parse_args(argv)
    try:
        result = generate(args.icao)
    except (TrendsError, OSError) as error:
        print(f"Trends update failed: {error}", file=os.sys.stderr)
        return 1
    print(f"requestedStations: {result['requestedStations']}")
    print(f"generatedStations: {result['generatedStations']}")
    for warning in result["warnings"]:
        print(f"warning: {warning}")
    print(f"output: {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
