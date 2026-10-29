#!/usr/bin/env python3
"""Continuously refresh local METAR, TAF, history, archive and trends data."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIVE_SCRIPT = PROJECT_ROOT / "scripts" / "update-metar.py"
HISTORY_SCRIPT = PROJECT_ROOT / "scripts" / "update-metar-history.py"
TAF_SCRIPT = PROJECT_ROOT / "scripts" / "update-taf.py"
RECENT_SCRIPT = PROJECT_ROOT / "scripts" / "update-metar-recent.py"
ARCHIVE_SCRIPT = PROJECT_ROOT / "scripts" / "update-metar-archive.py"
TRENDS_SCRIPT = PROJECT_ROOT / "scripts" / "update-metar-trends.py"
LIVE_PATH = PROJECT_ROOT / "data" / "metar-live.json"
HISTORY_PATH = PROJECT_ROOT / "data" / "metar-history.json"
TAF_PATH = PROJECT_ROOT / "data" / "taf-live.json"
RECENT_DIR = PROJECT_ROOT / "data" / "metar-recent"
ARCHIVE_DIR = PROJECT_ROOT / "data" / "metar-archive"
ARCHIVE_INDEX_PATH = ARCHIVE_DIR / "index.json"
TRENDS_DIR = PROJECT_ROOT / "data" / "metar-trends"
SCHEDULER_STATE_PATH = PROJECT_ROOT / "data" / ".weather-updater-state.json"
WEATHER_LOCK_PATH = PROJECT_ROOT / "data" / ".weather-updater.lock"
UPDATE_INTERVAL_SECONDS = 10 * 60
TAF_UPDATE_INTERVAL_SECONDS = 30 * 60
RECENT_UPDATE_INTERVAL_SECONDS = 60 * 60
ARCHIVE_UPDATE_INTERVAL_SECONDS = 10 * 60
TRENDS_UPDATE_INTERVAL_SECONDS = 60 * 60
ARCHIVE_RECONCILE_INTERVAL_SECONDS = 24 * 60 * 60
ARCHIVE_RECONCILE_DAYS = 7
ICAO_PATTERN = re.compile(r"^[A-Z]{4}$")
TAF_GROUP_TYPES = {"BASELINE", "FM", "TEMPO", "BECMG", "PROB", "PROB_TEMPO"}
TRENDS_METRIC_FIELDS = {
    "vfrPercent",
    "mvfrOrBetterPercent",
    "maxTemperatureC",
    "minTemperatureC",
    "relativeHumidityPercent",
    "windSpeedKt",
}
ARCHIVE_LOCK_CONFLICT_MARKER = "Another archive writer holds"

Validator = Callable[[Any], Tuple[bool, str]]


def load_archive_lock_class() -> Any:
    spec = importlib.util.spec_from_file_location(
        "weather_archive_lock", ARCHIVE_SCRIPT
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("archive lockmodule kon niet worden geladen")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ArchiveLock, module.ArchiveError


ArchiveLock, ArchiveLockError = load_archive_lock_class()


class ShutdownRequested(Exception):
    """Internal signal used for a clean SIGINT/SIGTERM shutdown."""


def timestamp(value: Optional[datetime] = None) -> str:
    moment = value or datetime.now().astimezone()
    return moment.isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"[{timestamp()}] {message}", flush=True)


def load_json(path: Path) -> Tuple[Optional[Any], Optional[bytes], str]:
    try:
        payload = path.read_bytes()
        return json.loads(payload.decode("utf-8")), payload, ""
    except FileNotFoundError:
        return None, None, "bestand bestaat niet"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, None, str(error)


def live_station_ids(document: Any) -> set[str]:
    if not isinstance(document, list):
        return set()
    return {
        str(record.get("icaoId", "")).strip().upper()
        for record in document
        if isinstance(record, dict)
        and ICAO_PATTERN.fullmatch(str(record.get("icaoId", "")).strip().upper())
    }


def validate_live(document: Any, minimum_records: int = 1) -> Tuple[bool, str]:
    if not isinstance(document, list):
        return False, "root is geen JSON-array"
    if len(document) < minimum_records:
        return False, f"slechts {len(document)} records; minimaal {minimum_records} vereist"

    ids: list[str] = []
    for index, record in enumerate(document):
        if not isinstance(record, dict):
            return False, f"record {index} is geen object"
        icao = str(record.get("icaoId", "")).strip().upper()
        if not ICAO_PATTERN.fullmatch(icao):
            return False, f"record {index} heeft geen geldige ICAO-code"
        if "rawOb" not in record:
            return False, f"record {icao} mist rawOb"
        ids.append(icao)
    if len(ids) != len(set(ids)):
        return False, "dubbele ICAO-records"
    return True, f"{len(document)} stations"


def validate_history(
    document: Any,
    expected_ids: Optional[set[str]] = None,
    minimum_observations: int = 1,
) -> Tuple[bool, str]:
    if not isinstance(document, dict):
        return False, "root is geen JSON-object"
    if document.get("schemaVersion") != 1:
        return False, "schemaVersion is niet 1"
    airports = document.get("airports")
    if not isinstance(airports, dict) or not airports:
        return False, "airports ontbreekt of is leeg"

    actual_ids = set(airports)
    if expected_ids is not None and actual_ids != expected_ids:
        missing = len(expected_ids - actual_ids)
        extra = len(actual_ids - expected_ids)
        return False, f"stationset wijkt af (ontbrekend={missing}, extra={extra})"

    required_fields = {"obsTime", "temp", "visib", "wspd", "wgst", "altim"}
    observation_count = 0
    for icao, station in airports.items():
        if not ICAO_PATTERN.fullmatch(icao) or not isinstance(station, dict):
            return False, f"ongeldige station-entry {icao!r}"
        if station.get("icaoId") != icao:
            return False, f"ICAO-mismatch bij {icao}"
        observations = station.get("observations")
        if not isinstance(observations, list):
            return False, f"observations van {icao} is geen array"
        times: list[str] = []
        for record in observations:
            if not isinstance(record, dict) or set(record) != required_fields:
                return False, f"ongeldig observatierecord bij {icao}"
            obs_time = record.get("obsTime")
            if not isinstance(obs_time, str) or not obs_time:
                return False, f"ongeldige obsTime bij {icao}"
            times.append(obs_time)
        if times != sorted(times) or len(times) != len(set(times)):
            return False, f"observaties van {icao} zijn niet uniek en chronologisch"
        observation_count += len(observations)

    if observation_count < minimum_observations:
        return False, (
            f"slechts {observation_count} observaties; "
            f"minimaal {minimum_observations} vereist"
        )
    return True, f"{len(airports)} stations, {observation_count} observaties"


def history_observation_count(document: Any) -> int:
    if not isinstance(document, dict) or not isinstance(document.get("airports"), dict):
        return 0
    return sum(
        len(station.get("observations", []))
        for station in document["airports"].values()
        if isinstance(station, dict) and isinstance(station.get("observations"), list)
    )


def parse_iso_time(value: Any) -> Optional[datetime]:
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
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def validate_taf(document: Any) -> Tuple[bool, str]:
    if not isinstance(document, dict):
        return False, "root is geen JSON-object"
    if document.get("schemaVersion") != 1:
        return False, "schemaVersion is niet 1"
    if parse_iso_time(document.get("generatedAt")) is None:
        return False, "generatedAt ontbreekt of is ongeldig"
    airports = document.get("airports")
    if not isinstance(airports, dict) or not airports:
        return False, "airports ontbreekt of is leeg"
    if document.get("availableStationCount") != len(airports):
        return False, "availableStationCount wijkt af"

    group_count = 0
    for icao, station in airports.items():
        if not ICAO_PATTERN.fullmatch(icao) or not isinstance(station, dict):
            return False, f"ongeldige station-entry {icao!r}"
        if station.get("icaoId") != icao or not str(station.get("rawTAF", "")).strip():
            return False, f"ongeldige ICAO/rawTAF bij {icao}"
        if parse_iso_time(station.get("issueTime")) is None:
            return False, f"ongeldige issueTime bij {icao}"
        valid_from = parse_iso_time(station.get("validFrom"))
        valid_to = parse_iso_time(station.get("validTo"))
        if valid_from is None or valid_to is None or valid_from >= valid_to:
            return False, f"ongeldige geldigheidsperiode bij {icao}"
        groups = station.get("forecastGroups")
        if not isinstance(groups, list) or not groups:
            return False, f"forecastGroups ontbreekt bij {icao}"
        for sequence, group in enumerate(groups):
            if not isinstance(group, dict):
                return False, f"ongeldige forecastgroep bij {icao}"
            if group.get("sequence") != sequence:
                return False, f"ongeldige groepsvolgorde bij {icao}"
            if group.get("type") not in TAF_GROUP_TYPES:
                return False, f"ongeldig groeptype bij {icao}"
            group_from = parse_iso_time(group.get("validFrom"))
            group_to = parse_iso_time(group.get("validTo"))
            if group_from is None or group_to is None or group_from >= group_to:
                return False, f"ongeldige groepstijd bij {icao}"
            group_count += 1
    return True, f"{len(airports)} stations, {group_count} forecastgroepen"


def taf_update_due(
    document: Any,
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    valid, detail = validate_taf(document)
    if not valid:
        return True, f"geen geldige TAF-snapshot ({detail})"
    generated_at = parse_iso_time(document.get("generatedAt"))
    assert generated_at is not None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age_seconds = (current.astimezone(timezone.utc) - generated_at).total_seconds()
    if age_seconds < 0:
        return True, "generatedAt ligt in de toekomst"
    if age_seconds >= TAF_UPDATE_INTERVAL_SECONDS:
        return True, f"laatste geslaagde TAF-update is {age_seconds / 60:.0f} minuten oud"
    remaining = max(0, TAF_UPDATE_INTERVAL_SECONDS - age_seconds)
    return False, f"TAF-update over circa {remaining / 60:.0f} minuten verschuldigd"


def utc_now(value: Optional[datetime] = None) -> datetime:
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def iso_timestamp(value: Optional[datetime] = None) -> str:
    return utc_now(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def age_due(
    last_success: Optional[datetime],
    interval_seconds: int,
    now: Optional[datetime] = None,
    missing_detail: str = "laatste succesvolle update ontbreekt",
) -> Tuple[bool, str]:
    if last_success is None:
        return True, missing_detail
    current = utc_now(now)
    age_seconds = (current - utc_now(last_success)).total_seconds()
    if age_seconds < 0:
        return True, "laatste succesvolle update ligt in de toekomst"
    if age_seconds >= interval_seconds:
        return True, f"laatste succesvolle update is {age_seconds / 60:.0f} minuten oud"
    due_at = utc_now(last_success) + timedelta(seconds=interval_seconds)
    return False, f"volgende update om {timestamp(due_at.astimezone())}"


def validate_recent_snapshot(
    directory: Path,
    expected_ids: set[str],
) -> Tuple[bool, str, Optional[datetime]]:
    if not expected_ids:
        return False, "geen geldige actuele stationset", None
    if not directory.is_dir():
        return False, "recent-historymap ontbreekt", None
    paths = {path.stem.upper(): path for path in directory.glob("*.json")}
    if set(paths) != expected_ids:
        return (
            False,
            f"stationset wijkt af (ontbrekend={len(expected_ids - set(paths))}, extra={len(set(paths) - expected_ids)})",
            None,
        )
    generated_times: list[datetime] = []
    for icao in sorted(expected_ids):
        document, _, error = load_json(paths[icao])
        if not isinstance(document, dict) or document.get("schemaVersion") != 1:
            return False, f"ongeldig recent-historyroot bij {icao}: {error}", None
        if document.get("icao") != icao:
            return False, f"ICAO-mismatch in recent-history bij {icao}", None
        generated = parse_iso_time(document.get("generatedAt"))
        observations = document.get("observations")
        if generated is None:
            return False, f"ongeldige generatedAt bij {icao}", None
        if not isinstance(observations, list) or document.get("recordCount") != len(observations):
            return False, f"ongeldige observations/recordCount bij {icao}", None
        generated_times.append(generated)
    return True, f"{len(paths)} stationfiles", min(generated_times)


def recent_update_due(
    directory: Path,
    expected_ids: set[str],
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    valid, detail, generated = validate_recent_snapshot(directory, expected_ids)
    if not valid:
        return True, f"geen geldige recente History-snapshot ({detail})"
    return age_due(generated, RECENT_UPDATE_INTERVAL_SECONDS, now)


def validate_archive_index(
    document: Any,
    expected_ids: Optional[set[str]] = None,
) -> Tuple[bool, str, Optional[datetime]]:
    if not isinstance(document, dict) or document.get("schemaVersion") != 1:
        return False, "ongeldige root/schemaVersion", None
    updated = parse_iso_time(document.get("updatedAt") or document.get("generatedAt"))
    stations = document.get("stations")
    if updated is None or not isinstance(stations, dict) or not stations:
        return False, "updatedAt/stations ontbreekt of is ongeldig", None
    if document.get("stationCount") != len(stations):
        return False, "stationCount wijkt af", None
    total = 0
    for icao, station in stations.items():
        if not ICAO_PATTERN.fullmatch(icao) or not isinstance(station, dict):
            return False, f"ongeldige station-entry {icao!r}", None
        count = station.get("recordCount")
        years = station.get("years")
        if not isinstance(count, int) or count < 0 or not isinstance(years, dict):
            return False, f"ongeldige indexsamenvatting bij {icao}", None
        total += count
    if document.get("recordCount") != total:
        return False, "recordCount wijkt af", None
    if expected_ids is not None and not expected_ids.issubset(set(stations)):
        return False, f"{len(expected_ids - set(stations))} actuele stations ontbreken", None
    return True, f"{len(stations)} stations, {total} observaties", updated


def archive_update_due(
    document: Any,
    expected_ids: set[str],
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    valid, detail, updated = validate_archive_index(document, expected_ids)
    if not valid:
        return True, f"geen geldige archive-index ({detail})"
    return age_due(updated, ARCHIVE_UPDATE_INTERVAL_SECONDS, now)


def validate_trends_snapshot(
    directory: Path,
    expected_ids: set[str],
) -> Tuple[bool, str, Optional[datetime]]:
    if not expected_ids:
        return False, "archive bevat geen stations", None
    if not directory.is_dir():
        return False, "Trends-map ontbreekt", None
    paths = {path.stem.upper(): path for path in directory.glob("*.json")}
    if set(paths) != expected_ids:
        return (
            False,
            f"stationset wijkt af (ontbrekend={len(expected_ids - set(paths))}, extra={len(set(paths) - expected_ids)})",
            None,
        )
    generated_times: list[datetime] = []
    for icao in sorted(expected_ids):
        document, _, error = load_json(paths[icao])
        if not isinstance(document, dict) or document.get("schemaVersion") != 1:
            return False, f"ongeldige Trends-root bij {icao}: {error}", None
        if document.get("icao") != icao or not isinstance(document.get("timezone"), str):
            return False, f"ICAO/timezone-mismatch bij {icao}", None
        generated = parse_iso_time(document.get("generatedAt"))
        coverage = document.get("coverage")
        years = document.get("years")
        if generated is None:
            return False, f"ongeldige generatedAt bij {icao}", None
        if not isinstance(coverage, dict):
            return False, f"coverage ontbreekt bij {icao}", None
        first = parse_iso_time(coverage.get("firstObservation"))
        last = parse_iso_time(coverage.get("lastObservation"))
        if first is None or last is None or first > last:
            return False, f"ongeldige coverage bij {icao}", None
        if not isinstance(years, dict) or not years:
            return False, f"years ontbreekt bij {icao}", None
        if not isinstance(document.get("definitions"), dict) or not isinstance(
            document.get("averages"), dict
        ):
            return False, f"definitions/averages ontbreekt bij {icao}", None
        for year, months in years.items():
            if not re.fullmatch(r"\d{4}", str(year)) or not isinstance(months, dict):
                return False, f"ongeldig jaar bij {icao}", None
            for month, values in months.items():
                if (
                    not str(month).isdigit()
                    or not 1 <= int(month) <= 12
                    or not isinstance(values, dict)
                ):
                    return False, f"ongeldige maand bij {icao}", None
                if not isinstance(values.get("partialMonth"), bool) or not isinstance(
                    values.get("quality"), dict
                ):
                    return False, f"ongeldige maandkwaliteit bij {icao}", None
                for field in TRENDS_METRIC_FIELDS:
                    value = values.get(field)
                    if value is not None and (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                    ):
                        return False, f"ongeldige {field} bij {icao}", None
        generated_times.append(generated)
    return True, f"{len(paths)} stationfiles", min(generated_times)


def trends_update_due(
    directory: Path,
    expected_ids: set[str],
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    valid, detail, generated = validate_trends_snapshot(directory, expected_ids)
    if not valid:
        return True, f"geen geldige Trends-snapshot ({detail})"
    return age_due(generated, TRENDS_UPDATE_INTERVAL_SECONDS, now)


def load_scheduler_state(path: Path = SCHEDULER_STATE_PATH) -> Dict[str, Any]:
    document, _, _ = load_json(path)
    if not isinstance(document, dict) or document.get("schemaVersion") != 1:
        return {"schemaVersion": 1, "archiveLastReconciledAt": None}
    return {
        "schemaVersion": 1,
        "archiveLastReconciledAt": document.get("archiveLastReconciledAt"),
    }


def write_scheduler_state_atomic(
    document: Dict[str, Any], path: Path = SCHEDULER_STATE_PATH
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.stem}-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            json.dump(document, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        path.chmod(0o644)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def archive_reconciliation_due(
    state: Any,
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    last = (
        parse_iso_time(state.get("archiveLastReconciledAt"))
        if isinstance(state, dict)
        else None
    )
    return age_due(
        last,
        ARCHIVE_RECONCILE_INTERVAL_SECONDS,
        now,
        missing_detail="nog geen succesvolle archive-reconciliatie geregistreerd",
    )


def atomic_restore(path: Path, payload: bytes) -> None:
    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.stem}-restore-",
            suffix=path.suffix,
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        path.chmod(0o644)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def protect_previous_output(
    path: Path,
    previous_payload: Optional[bytes],
    previous_was_valid: bool,
) -> None:
    if previous_was_valid and previous_payload is not None:
        atomic_restore(path, previous_payload)
        return
    try:
        current, _, _ = load_json(path)
        if current is None and path.exists():
            path.unlink()
    except OSError as error:
        log(f"WAARSCHUWING: ongeldige nieuwe output kon niet worden verwijderd: {error}")


def run_update(
    label: str,
    script: Path,
    output_path: Path,
    validator: Validator,
    previous_validator: Optional[Validator] = None,
) -> bool:
    started = time.monotonic()
    log(f"{label}-update gestart")
    previous_document, previous_payload, previous_error = load_json(output_path)
    previous_valid, previous_detail = (previous_validator or validator)(
        previous_document
    )
    if not previous_valid:
        previous_payload = None
        log(
            f"{label}: geen geldige vorige snapshot beschikbaar "
            f"({previous_detail or previous_error})"
        )

    try:
        completed = subprocess.run(
            [sys.executable, str(script)],
            cwd=PROJECT_ROOT,
            check=False,
        )
    except OSError as error:
        duration = time.monotonic() - started
        log(f"{label}-update mislukt na {duration:.1f}s: proces kon niet starten: {error}")
        protect_previous_output(output_path, previous_payload, previous_valid)
        return False

    new_document, _, load_error = load_json(output_path)
    new_valid, new_detail = validator(new_document)
    if completed.returncode != 0 or not new_valid:
        reason = (
            f"exitcode {completed.returncode}"
            if completed.returncode != 0
            else new_detail or load_error
        )
        protect_previous_output(output_path, previous_payload, previous_valid)
        duration = time.monotonic() - started
        log(f"{label}-update mislukt na {duration:.1f}s: {reason}; vorige geldige data behouden")
        return False

    duration = time.monotonic() - started
    log(f"{label}-update geslaagd in {duration:.1f}s: {new_detail}")
    return True


def concise_process_error(completed: subprocess.CompletedProcess[str]) -> str:
    lines = [
        line.strip()
        for line in f"{completed.stderr or ''}\n{completed.stdout or ''}".splitlines()
        if line.strip()
    ]
    for line in reversed(lines):
        if any(marker in line.lower() for marker in ("fail", "error", "mislukt")):
            return line
    return lines[-1] if lines else f"exitcode {completed.returncode}"


def run_external_task(
    label: str,
    command: Sequence[str],
    validator: Callable[[], Tuple[bool, str]],
    lock_conflict_is_skip: bool = False,
) -> str:
    """Run a self-protecting updater and return success, failure or skipped."""
    started = time.monotonic()
    log(f"{label}-update gestart")
    try:
        completed = subprocess.run(
            [sys.executable, *command],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        duration = time.monotonic() - started
        log(f"{label}-update mislukt na {duration:.1f}s: proces kon niet starten: {error}")
        return "failure"
    combined_output = f"{completed.stderr or ''}\n{completed.stdout or ''}"
    if (
        lock_conflict_is_skip
        and completed.returncode != 0
        and ARCHIVE_LOCK_CONFLICT_MARKER in combined_output
    ):
        duration = time.monotonic() - started
        log(f"{label}-update veilig overgeslagen na {duration:.1f}s: archive-lock is actief")
        return "skipped"
    valid, detail = validator()
    if completed.returncode != 0 or not valid:
        reason = concise_process_error(completed) if completed.returncode != 0 else detail
        duration = time.monotonic() - started
        log(f"{label}-update mislukt na {duration:.1f}s: {reason}")
        return "failure"
    duration = time.monotonic() - started
    log(f"{label}-update geslaagd in {duration:.1f}s: {detail}")
    return "success"


def run_cycle(now: Optional[datetime] = None) -> bool:
    log("Updatecyclus gestart")
    fixed_test_time = utc_now(now) if now is not None else None

    def decision_time() -> datetime:
        return fixed_test_time or utc_now()

    previous_live, _, _ = load_json(LIVE_PATH)
    previous_live_count = len(previous_live) if isinstance(previous_live, list) else 0
    minimum_live_records = max(1, int(previous_live_count * 0.8))
    try:
        live_ok = run_update(
            "Live",
            LIVE_SCRIPT,
            LIVE_PATH,
            lambda document: validate_live(document, minimum_live_records),
        )
    except Exception as error:
        log(f"Live-update onverwacht mislukt: {error}")
        live_ok = False
    history_ok = True
    if not live_ok:
        log("History-update overgeslagen omdat de live-update niet is geslaagd")
        history_ok = False
    else:
        current_live, _, _ = load_json(LIVE_PATH)
        expected_ids = live_station_ids(current_live)
        previous_history, _, _ = load_json(HISTORY_PATH)
        previous_history_count = history_observation_count(previous_history)
        minimum_history_observations = max(1, int(previous_history_count * 0.8))
        try:
            history_ok = run_update(
                "History",
                HISTORY_SCRIPT,
                HISTORY_PATH,
                lambda document: validate_history(
                    document, expected_ids, minimum_history_observations
                ),
                previous_validator=validate_history,
            )
        except Exception as error:
            log(f"History-update onverwacht mislukt: {error}")
            history_ok = False

    current_taf, _, _ = load_json(TAF_PATH)
    taf_due, taf_due_detail = taf_update_due(current_taf, decision_time())
    taf_ok = True
    if taf_due:
        log(f"TAF-update verschuldigd: {taf_due_detail}")
        try:
            taf_ok = run_update(
                "TAF",
                TAF_SCRIPT,
                TAF_PATH,
                validate_taf,
            )
        except Exception as error:
            log(f"TAF-update onverwacht mislukt: {error}")
            taf_ok = False
    else:
        log(f"TAF-update overgeslagen: {taf_due_detail}")

    current_live, _, _ = load_json(LIVE_PATH)
    expected_ids = live_station_ids(current_live)

    recent_due, recent_detail = recent_update_due(
        RECENT_DIR, expected_ids, decision_time()
    )
    recent_ok = True
    if recent_due:
        log(f"Recent History-update verschuldigd: {recent_detail}")
        try:
            recent_status = run_external_task(
                "Recent History",
                [str(RECENT_SCRIPT)],
                lambda: validate_recent_snapshot(RECENT_DIR, expected_ids)[:2],
            )
            recent_ok = recent_status == "success"
        except Exception as error:
            log(f"Recent History-update onverwacht mislukt: {error}")
            recent_ok = False
    else:
        log(f"Recent History-update overgeslagen: {recent_detail}")

    archive_document, _, _ = load_json(ARCHIVE_INDEX_PATH)
    archive_valid, archive_validation_detail, _archive_updated = validate_archive_index(
        archive_document, expected_ids
    )
    archive_due, archive_due_detail = archive_update_due(
        archive_document, expected_ids, decision_time()
    )
    state = load_scheduler_state(SCHEDULER_STATE_PATH)
    reconcile_due, reconcile_detail = archive_reconciliation_due(state, decision_time())
    archive_ok = True
    archive_command: Optional[list[str]] = None
    reconciliation_run = False
    if not archive_valid:
        # Default archive behavior performs a 30-day backfill only for stations
        # that have no archive yet; it uses 48 hours for existing stations.
        archive_command = [str(ARCHIVE_SCRIPT)]
        log(f"Archive-update verschuldigd: {archive_validation_detail}")
    elif reconcile_due:
        archive_command = [
            str(ARCHIVE_SCRIPT),
            "--reconcile-days",
            str(ARCHIVE_RECONCILE_DAYS),
        ]
        reconciliation_run = True
        log(f"Archive-reconciliatie verschuldigd: {reconcile_detail}")
    elif archive_due:
        archive_command = [str(ARCHIVE_SCRIPT)]
        log(f"Archive-update verschuldigd: {archive_due_detail}")
    else:
        log(
            "Archive-update overgeslagen: "
            f"{archive_due_detail}; reconciliatie: {reconcile_detail}"
        )

    if archive_command is not None:
        try:
            archive_status = run_external_task(
                "Archive-reconciliatie" if reconciliation_run else "Archive",
                archive_command,
                lambda: validate_archive_index(
                    load_json(ARCHIVE_INDEX_PATH)[0], expected_ids
                )[:2],
                lock_conflict_is_skip=True,
            )
            archive_ok = archive_status != "failure"
            if archive_status == "success" and reconciliation_run:
                state["archiveLastReconciledAt"] = iso_timestamp()
                try:
                    write_scheduler_state_atomic(state, SCHEDULER_STATE_PATH)
                except OSError as error:
                    log(
                        "Archive-reconciliatie was succesvol, maar scheduler-state "
                        f"kon niet worden opgeslagen: {error}"
                    )
                    archive_ok = False
        except Exception as error:
            log(f"Archive-update onverwacht mislukt: {error}")
            archive_ok = False

    refreshed_archive, _, _ = load_json(ARCHIVE_INDEX_PATH)
    refreshed_archive_valid, refreshed_archive_detail, _ = validate_archive_index(
        refreshed_archive, expected_ids
    )
    trends_ok = True
    trends_expected_ids = (
        set(refreshed_archive["stations"])
        if refreshed_archive_valid and isinstance(refreshed_archive.get("stations"), dict)
        else set()
    )
    if not refreshed_archive_valid:
        log(
            "Trends-update veilig overgeslagen: archive is niet bruikbaar "
            f"({refreshed_archive_detail})"
        )
    else:
        trends_due, trends_detail = trends_update_due(
            TRENDS_DIR, trends_expected_ids, decision_time()
        )
        if trends_due:
            log(f"Trends-update verschuldigd: {trends_detail}")
            try:
                trends_status = run_external_task(
                    "Trends",
                    [str(TRENDS_SCRIPT)],
                    lambda: validate_trends_snapshot(
                        TRENDS_DIR, trends_expected_ids
                    )[:2],
                )
                trends_ok = trends_status == "success"
            except Exception as error:
                log(f"Trends-update onverwacht mislukt: {error}")
                trends_ok = False
        else:
            log(f"Trends-update overgeslagen: {trends_detail}")

    # Re-read timestamps after any successful task so the next due moment is clear.
    planning_time = utc_now()
    refreshed_taf, _, _ = load_json(TAF_PATH)
    log(f"TAF-planning: {taf_update_due(refreshed_taf, planning_time)[1]}")
    log(f"Recent History-planning: {recent_update_due(RECENT_DIR, expected_ids, planning_time)[1]}")
    log(f"Archive-planning: {archive_update_due(refreshed_archive, expected_ids, planning_time)[1]}")
    refreshed_state = load_scheduler_state(SCHEDULER_STATE_PATH)
    log(f"Archive-reconciliatieplanning: {archive_reconciliation_due(refreshed_state, planning_time)[1]}")
    if refreshed_archive_valid:
        log(f"Trends-planning: {trends_update_due(TRENDS_DIR, trends_expected_ids, planning_time)[1]}")
    else:
        log(f"Trends-planning: niet beschikbaar ({refreshed_archive_detail})")

    return live_ok and history_ok and taf_ok and recent_ok and archive_ok and trends_ok


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update all local weather datasets while development is running."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one immediate update cycle and exit",
    )
    return parser.parse_args(argv)


def shutdown_signal_handler(signum: int, _frame: Any) -> None:
    raise ShutdownRequested(f"signal {signum}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    previous_handlers: Dict[int, Any] = {}
    try:
        with ArchiveLock(path=WEATHER_LOCK_PATH):
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, shutdown_signal_handler)
            log(
                "Automatische weather-updater gestart "
                f"(METAR/history/archive: {UPDATE_INTERVAL_SECONDS // 60} min; "
                f"TAF: {TAF_UPDATE_INTERVAL_SECONDS // 60} min; "
                f"recent History: {RECENT_UPDATE_INTERVAL_SECONDS // 60} min; "
                f"Trends: {TRENDS_UPDATE_INTERVAL_SECONDS // 60} min; "
                f"archive-reconciliatie: {ARCHIVE_RECONCILE_INTERVAL_SECONDS // 3600} uur)"
            )
            while True:
                cycle_started = time.monotonic()
                cycle_ok = False
                try:
                    cycle_ok = run_cycle()
                except (KeyboardInterrupt, ShutdownRequested):
                    raise
                except Exception as error:  # Keep the development updater alive.
                    log(f"Onverwachte fout in updatecyclus: {error}")

                if args.once:
                    log("Eenmalige updatecyclus voltooid")
                    return 0 if cycle_ok else 1

                wait_seconds = max(
                    0.0, UPDATE_INTERVAL_SECONDS - (time.monotonic() - cycle_started)
                )
                next_update = datetime.now().astimezone() + timedelta(seconds=wait_seconds)
                log(
                    "Volgende live/history/archive-cyclus gepland voor "
                    f"{timestamp(next_update)}"
                )
                time.sleep(wait_seconds)
    except ArchiveLockError as error:
        log(f"Automatische weather-updater niet gestart: {error}")
        return 1
    except (KeyboardInterrupt, ShutdownRequested):
        print()
        log("Stop ontvangen; automatische weather-updater netjes afgesloten")
        return 0
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
