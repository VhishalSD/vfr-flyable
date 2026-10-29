#!/usr/bin/env python3
"""Maintain a permanent, UTC-month-partitioned METAR/SPECI archive."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RECENT_UPDATER_PATH = PROJECT_ROOT / "scripts" / "update-metar-recent.py"
LIVE_PATH = PROJECT_ROOT / "data" / "metar-live.json"
ARCHIVE_DIR = PROJECT_ROOT / "data" / "metar-archive"
INDEX_PATH = ARCHIVE_DIR / "index.json"
LOCK_PATH = PROJECT_ROOT / "data" / ".metar-archive.lock"

SCHEMA_VERSION = 1
INDEX_SCHEMA_VERSION = 1
INITIAL_BACKFILL_DAYS = 30.0
INCREMENTAL_OVERLAP_HOURS = 48.0
RECONCILIATION_DAYS = 7.0
MAX_BACKFILL_DAYS = 30.0
MAX_SEGMENT_HOURS = 168.0
DEFAULT_WORKERS = 6
REQUEST_INTERVAL_SECONDS = 0.65
LOCK_STALE_SECONDS = 6 * 60 * 60
USER_AGENT = "vfr-flyable-school-project/1.0 (permanent METAR archive updater)"


class ArchiveError(RuntimeError):
    """Raised when an archive operation cannot be completed safely."""


def load_recent_updater() -> Any:
    spec = importlib.util.spec_from_file_location("metar_recent_shared", RECENT_UPDATER_PATH)
    if spec is None or spec.loader is None:
        raise ArchiveError("Cannot load shared METAR normalization module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.USER_AGENT = USER_AGENT
    return module


recent = load_recent_updater()
BASE_OBSERVATION_FIELDS = frozenset(recent.OBSERVATION_FIELDS)
LEGACY_OBSERVATION_FIELDS = frozenset(recent.LEGACY_OBSERVATION_FIELDS)
PARTITION_SOURCE = {
    "provider": "NOAA Aviation Weather Center",
    "endpoint": recent.SOURCE_URL,
    "partitioning": "UTC calendar month",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return recent.iso_utc(value)


def parse_timestamp(value: Any) -> Optional[datetime]:
    return recent.parse_timestamp(value)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def json_bytes(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def write_fsynced(path: Path, payload: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o644)


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ArchiveLock(AbstractContextManager["ArchiveLock"]):
    """Exclusive writer lock with PID/host-aware stale lock recovery."""

    def __init__(
        self,
        path: Path = LOCK_PATH,
        stale_seconds: float = LOCK_STALE_SECONDS,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.path = path
        self.stale_seconds = stale_seconds
        self.now = now
        self.token = uuid.uuid4().hex
        self.acquired = False

    def _document(self) -> Dict[str, Any]:
        return {
            "schemaVersion": 1,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "startedAt": iso_utc(self.now()),
            "token": self.token,
        }

    def _is_stale(self, payload: bytes, stat_result: os.stat_result) -> bool:
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return (time.time() - stat_result.st_mtime) > self.stale_seconds
        started = parse_timestamp(document.get("startedAt")) if isinstance(document, dict) else None
        age = (
            (self.now() - started).total_seconds()
            if started is not None
            else time.time() - stat_result.st_mtime
        )
        same_host = document.get("hostname") == socket.gethostname()
        pid = document.get("pid")
        if same_host and isinstance(pid, int):
            return not process_is_running(pid)
        return age > self.stale_seconds

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                try:
                    before = self.path.stat()
                    payload = self.path.read_bytes()
                    after = self.path.stat()
                except FileNotFoundError:
                    continue
                if (before.st_ino, before.st_mtime_ns, before.st_size) != (
                    after.st_ino,
                    after.st_mtime_ns,
                    after.st_size,
                ):
                    continue
                if not self._is_stale(payload, after):
                    raise ArchiveError(f"Another archive writer holds {self.path}")
                try:
                    current = self.path.stat()
                    if (current.st_ino, current.st_mtime_ns, current.st_size) != (
                        after.st_ino,
                        after.st_mtime_ns,
                        after.st_size,
                    ):
                        continue
                    self.path.unlink()
                    fsync_directory(self.path.parent)
                except FileNotFoundError:
                    pass
                continue
            try:
                payload = json_bytes(self._document())
                written = 0
                while written < len(payload):
                    written += os.write(descriptor, payload[written:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            fsync_directory(self.path.parent)
            self.acquired = True
            return
        raise ArchiveError(f"Could not safely acquire archive lock {self.path}")

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if document.get("token") == self.token:
                self.path.unlink()
                fsync_directory(self.path.parent)
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        self.acquired = False

    def __enter__(self) -> "ArchiveLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.release()


def base_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: record.get(key) for key in BASE_OBSERVATION_FIELDS}


def validate_variant(record: Any, expected_time: Optional[str] = None) -> Tuple[bool, str]:
    if not isinstance(record, dict) or set(record) not in {
        LEGACY_OBSERVATION_FIELDS,
        BASE_OBSERVATION_FIELDS,
    }:
        return False, "revision/base observation fields do not match schema"
    valid, detail = recent.validate_observation(record)
    if not valid:
        return False, detail
    if expected_time is not None and record.get("obsTime") != expected_time:
        return False, "revision obsTime differs from canonical observation"
    return True, "valid"


def validate_archive_observation(record: Any) -> Tuple[bool, str]:
    if not isinstance(record, dict):
        return False, "observation is not an object"
    if set(record) not in {
        LEGACY_OBSERVATION_FIELDS,
        BASE_OBSERVATION_FIELDS,
        LEGACY_OBSERVATION_FIELDS | {"revisions"},
        BASE_OBSERVATION_FIELDS | {"revisions"},
    }:
        return False, "archive observation fields do not match schema"
    valid, detail = validate_variant(base_record(record))
    if not valid:
        return False, detail
    revisions = record.get("revisions", [])
    if not isinstance(revisions, list):
        return False, "revisions is not an array"
    seen_raw = {record["rawOb"]}
    for revision in revisions:
        valid, detail = validate_variant(revision, record["obsTime"])
        if not valid:
            return False, detail
        if revision["rawOb"] in seen_raw:
            return False, "duplicate rawOb across canonical/revisions"
        seen_raw.add(revision["rawOb"])
    return True, "valid"


def partition_key_for_time(value: Any) -> Tuple[int, int]:
    observed = parse_timestamp(value)
    if observed is None:
        raise ArchiveError(f"Invalid observation timestamp: {value!r}")
    return observed.year, observed.month


def partition_path(root: Path, icao: str, year: int, month: int) -> Path:
    return root / icao / f"{year:04d}" / f"{month:02d}.json"


def validate_partition(
    document: Any,
    expected_icao: Optional[str] = None,
    expected_year: Optional[int] = None,
    expected_month: Optional[int] = None,
) -> Tuple[bool, str]:
    if not isinstance(document, dict) or document.get("schemaVersion") != SCHEMA_VERSION:
        return False, "invalid partition root/schemaVersion"
    icao = document.get("icao")
    year = document.get("year")
    month = document.get("month")
    if not isinstance(icao, str) or not recent.ICAO_PATTERN.fullmatch(icao):
        return False, "invalid ICAO"
    if expected_icao is not None and icao != expected_icao:
        return False, "partition ICAO/path mismatch"
    if not isinstance(year, int) or not isinstance(month, int) or not 1 <= month <= 12:
        return False, "invalid year/month"
    if expected_year is not None and year != expected_year:
        return False, "partition year/path mismatch"
    if expected_month is not None and month != expected_month:
        return False, "partition month/path mismatch"
    if parse_timestamp(document.get("updatedAt")) is None:
        return False, "invalid updatedAt"
    observations = document.get("observations")
    if not isinstance(observations, list) or document.get("recordCount") != len(observations):
        return False, "invalid observations/recordCount"
    previous: Optional[datetime] = None
    for record in observations:
        valid, detail = validate_archive_observation(record)
        if not valid:
            return False, detail
        observed = parse_timestamp(record["obsTime"])
        assert observed is not None
        if (observed.year, observed.month) != (year, month):
            return False, "observation outside UTC partition"
        if previous is not None and observed <= previous:
            return False, "observations are not unique and chronological"
        previous = observed
    earliest = observations[0]["obsTime"] if observations else None
    latest = observations[-1]["obsTime"] if observations else None
    if document.get("earliestObsTime") != earliest or document.get("latestObsTime") != latest:
        return False, "partition time summary mismatch"
    return True, f"{icao} {year:04d}-{month:02d}: {len(observations)} observations"


PartitionKey = Tuple[str, int, int]


def load_archive(root: Path = ARCHIVE_DIR) -> Dict[PartitionKey, Dict[str, Any]]:
    if not root.exists():
        return {}
    if not root.is_dir():
        raise ArchiveError(f"Archive path is not a directory: {root}")
    documents: Dict[PartitionKey, Dict[str, Any]] = {}
    for path in sorted(root.glob("[A-Z][A-Z][A-Z][A-Z]/[0-9][0-9][0-9][0-9]/[0-9][0-9].json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ArchiveError(f"Corrupt existing archive partition; nothing changed: {path}: {error}") from error
        icao = path.parents[1].name
        try:
            year, month = int(path.parent.name), int(path.stem)
        except ValueError as error:
            raise ArchiveError(f"Invalid partition path: {path}") from error
        valid, detail = validate_partition(document, icao, year, month)
        if not valid:
            raise ArchiveError(f"Invalid existing archive partition; nothing changed: {path}: {detail}")
        key = (icao, year, month)
        if key in documents:
            raise ArchiveError(f"Duplicate archive partition: {path}")
        documents[key] = document
    index_path = root / "index.json"
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ArchiveError(f"Corrupt existing archive index; nothing changed: {error}") from error
        valid, detail = validate_index(index, documents)
        if not valid:
            raise ArchiveError(f"Invalid existing archive index; nothing changed: {detail}")
    return documents


def record_rank(record: Mapping[str, Any]) -> Tuple[Any, ...]:
    receipt = parse_timestamp(record.get("receiptTime"))
    completeness = sum(
        value is not None and value != []
        for key, value in record.items()
        if key not in {"rawOb", "revisions"}
    )
    return (
        1 if receipt is not None else 0,
        receipt.timestamp() if receipt is not None else float("-inf"),
        completeness,
        len(str(record.get("rawOb", ""))),
        str(record.get("rawOb", "")),
    )


def flatten_variants(record: Mapping[str, Any]) -> Iterable[Dict[str, Any]]:
    yield base_record(record)
    for revision in record.get("revisions", []):
        yield dict(revision)


def merge_observations(
    existing: Sequence[Mapping[str, Any]], incoming: Sequence[Mapping[str, Any]]
) -> Tuple[list[Dict[str, Any]], Dict[str, int]]:
    variants: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    stats = Counter()

    def add(record: Mapping[str, Any], source: str) -> None:
        valid, detail = validate_variant(base_record(record))
        if not valid:
            raise ArchiveError(f"Invalid {source} observation: {detail}")
        timestamp = str(record["obsTime"])
        raw = str(record["rawOb"])
        bucket = variants[timestamp]
        previous = bucket.get(raw)
        if previous is not None:
            stats["exactDuplicates"] += 1
            if record_rank(record) > record_rank(previous):
                bucket[raw] = base_record(record)
            return
        if bucket and source == "incoming":
            stats["correctionVariants"] += 1
        bucket[raw] = base_record(record)
        if source == "incoming":
            stats["newVariants"] += 1

    for canonical in existing:
        for variant in flatten_variants(canonical):
            add(variant, "existing")
    for record in incoming:
        add(record, "incoming")

    result: list[Dict[str, Any]] = []
    for timestamp in sorted(variants, key=lambda value: parse_timestamp(value)):
        candidates = sorted(variants[timestamp].values(), key=record_rank, reverse=True)
        canonical = dict(candidates[0])
        if len(candidates) > 1:
            canonical["revisions"] = [dict(item) for item in candidates[1:]]
        result.append(canonical)
    stats["logicalObservations"] = len(result)
    return result, dict(stats)


def new_partition(
    icao: str, year: int, month: int, observations: Sequence[Dict[str, Any]], updated_at: datetime
) -> Dict[str, Any]:
    records = list(observations)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "icao": icao,
        "year": year,
        "month": month,
        "updatedAt": iso_utc(updated_at),
        "source": dict(PARTITION_SOURCE),
        "recordCount": len(records),
        "earliestObsTime": records[0]["obsTime"] if records else None,
        "latestObsTime": records[-1]["obsTime"] if records else None,
        "observations": records,
    }


def merge_station_records(
    icao: str,
    source_records: Sequence[Mapping[str, Any]],
    documents: Mapping[PartitionKey, Dict[str, Any]],
    updated_at: datetime,
) -> Tuple[Dict[PartitionKey, Dict[str, Any]], Dict[str, int]]:
    incoming_by_partition: Dict[Tuple[int, int], list[Dict[str, Any]]] = defaultdict(list)
    for source in source_records:
        try:
            _, normalized, _ = recent.normalize_record(dict(source), icao)
        except (recent.UpdateError, ValueError) as error:
            raise ArchiveError(f"Invalid AWC record for {icao}: {error}") from error
        incoming_by_partition[partition_key_for_time(normalized["obsTime"])].append(normalized)

    changed: Dict[PartitionKey, Dict[str, Any]] = {}
    totals = Counter()
    for (year, month), incoming in sorted(incoming_by_partition.items()):
        key = (icao, year, month)
        existing_document = documents.get(key)
        existing = existing_document["observations"] if existing_document else []
        merged, stats = merge_observations(existing, incoming)
        totals.update(stats)
        if existing_document is None or merged != existing:
            document = new_partition(icao, year, month, merged, updated_at)
            valid, detail = validate_partition(document, icao, year, month)
            if not valid:
                raise ArchiveError(f"Generated partition is invalid: {detail}")
            changed[key] = document
    return changed, dict(totals)


def build_index(
    documents: Mapping[PartitionKey, Mapping[str, Any]], updated_at: datetime
) -> Dict[str, Any]:
    stations: Dict[str, Any] = {}
    total_records = 0
    global_earliest: Optional[str] = None
    global_latest: Optional[str] = None
    by_station: Dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for (icao, _year, _month), document in documents.items():
        by_station[icao].append(document)
    for icao, partitions in sorted(by_station.items()):
        partitions.sort(key=lambda item: (item["year"], item["month"]))
        years: Dict[str, list[str]] = defaultdict(list)
        count = 0
        earliest: Optional[str] = None
        latest: Optional[str] = None
        for partition in partitions:
            years[str(partition["year"])].append(f"{partition['month']:02d}")
            count += partition["recordCount"]
            part_earliest = partition["earliestObsTime"]
            part_latest = partition["latestObsTime"]
            if part_earliest is not None and (earliest is None or part_earliest < earliest):
                earliest = part_earliest
            if part_latest is not None and (latest is None or part_latest > latest):
                latest = part_latest
        stations[icao] = {
            "years": dict(years),
            "earliestObsTime": earliest,
            "latestObsTime": latest,
            "recordCount": count,
        }
        total_records += count
        if earliest is not None and (global_earliest is None or earliest < global_earliest):
            global_earliest = earliest
        if latest is not None and (global_latest is None or latest > global_latest):
            global_latest = latest
    return {
        "schemaVersion": INDEX_SCHEMA_VERSION,
        "generatedAt": iso_utc(updated_at),
        "updatedAt": iso_utc(updated_at),
        "source": {
            "provider": "NOAA Aviation Weather Center",
            "endpoint": recent.SOURCE_URL,
            "maximumHistoricalDays": 30,
            "canonicalKey": "ICAO + obsTime",
            "correctionPolicy": "latest receiptTime; deterministic completeness fallback; prior raw variants retained",
        },
        "partitioning": "ICAO/UTC-year/UTC-month",
        "stationCount": len(stations),
        "recordCount": total_records,
        "earliestObsTime": global_earliest,
        "latestObsTime": global_latest,
        "stations": stations,
    }


def validate_index(
    document: Any, partitions: Mapping[PartitionKey, Mapping[str, Any]]
) -> Tuple[bool, str]:
    if not isinstance(document, dict) or document.get("schemaVersion") != INDEX_SCHEMA_VERSION:
        return False, "invalid index root/schemaVersion"
    generated = parse_timestamp(document.get("generatedAt"))
    updated = parse_timestamp(document.get("updatedAt"))
    if generated is None or updated is None:
        return False, "invalid index timestamps"
    expected = build_index(partitions, updated)
    for key in (
        "partitioning",
        "stationCount",
        "recordCount",
        "earliestObsTime",
        "latestObsTime",
        "stations",
    ):
        if document.get(key) != expected.get(key):
            return False, f"index {key} does not match partitions"
    return True, f"{document['stationCount']} stations, {document['recordCount']} observations"


def stage_file(target: Path, document: Mapping[str, Any]) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{target.stem}-", suffix=".tmp", dir=target.parent)
    os.close(descriptor)
    path = Path(name)
    try:
        write_fsynced(path, json_bytes(document))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def backup_file(target: Path, token: str) -> Optional[Path]:
    if not target.exists():
        return None
    backup = target.with_name(f".{target.name}.backup-{token}")
    try:
        os.link(target, backup)
    except OSError:
        shutil.copy2(target, backup)
        with backup.open("rb") as handle:
            os.fsync(handle.fileno())
    return backup


def publish_transaction(
    root: Path,
    current: Mapping[PartitionKey, Dict[str, Any]],
    changed: Mapping[PartitionKey, Dict[str, Any]],
    index: Dict[str, Any],
    replace: Callable[[os.PathLike[str], os.PathLike[str]], None] = os.replace,
) -> None:
    valid, detail = validate_index(index, {**current, **changed})
    if not valid:
        raise ArchiveError(f"Refusing to publish invalid index: {detail}")
    root.mkdir(parents=True, exist_ok=True)
    targets: list[Tuple[Path, Mapping[str, Any]]] = [
        (partition_path(root, icao, year, month), document)
        for (icao, year, month), document in sorted(changed.items())
    ]
    targets.append((root / "index.json", index))
    token = uuid.uuid4().hex
    staged: Dict[Path, Path] = {}
    backups: Dict[Path, Optional[Path]] = {}
    published: list[Path] = []
    try:
        for target, document in targets:
            staged[target] = stage_file(target, document)
            backups[target] = backup_file(target, token)
        # The manifest is intentionally last.
        for target, _document in targets:
            replace(staged[target], target)
            published.append(target)
            fsync_directory(target.parent)
        fsync_directory(root)
    except Exception:
        for target in reversed(published):
            backup = backups.get(target)
            if backup is not None and backup.exists():
                os.replace(backup, target)
            else:
                target.unlink(missing_ok=True)
            fsync_directory(target.parent)
        raise
    finally:
        for path in staged.values():
            path.unlink(missing_ok=True)
        for path in backups.values():
            if path is not None:
                path.unlink(missing_ok=True)


def station_has_archive(icao: str, documents: Mapping[PartitionKey, Any]) -> bool:
    return any(key[0] == icao for key in documents)


def fetch_station_window(
    icao: str,
    start: datetime,
    end: datetime,
    limiter: Any,
    stats: Any,
    maximum_segment_hours: float = MAX_SEGMENT_HOURS,
) -> list[Dict[str, Any]]:
    fetcher = lambda station, segment_start, segment_end: recent.fetch_segment(
        station, segment_start, segment_end, limiter, stats
    )
    records: list[Dict[str, Any]] = []
    for segment_start, segment_end in recent.chunk_window(start, end, maximum_segment_hours):
        records.extend(
            recent.fetch_window_segmented(icao, segment_start, segment_end, fetcher, stats)
        )
    return records


def update_archive(
    selected_ids: Optional[Sequence[str]] = None,
    backfill_days: Optional[float] = None,
    reconcile_days: Optional[float] = None,
    workers: int = DEFAULT_WORKERS,
    request_interval: float = REQUEST_INTERVAL_SECONDS,
    maximum_segment_hours: float = MAX_SEGMENT_HOURS,
    root: Path = ARCHIVE_DIR,
    now: Optional[datetime] = None,
    fetch_station: Callable[..., list[Dict[str, Any]]] = fetch_station_window,
) -> Dict[str, Any]:
    station_ids = recent.load_station_ids(LIVE_PATH)
    requested = sorted(set(selected_ids or station_ids))
    outside = sorted(set(requested) - set(station_ids))
    if outside:
        raise ArchiveError(f"Requested ICAO outside current station set: {', '.join(outside)}")
    if not requested:
        raise ArchiveError("No stations requested")
    if backfill_days is not None and not 0 < backfill_days <= MAX_BACKFILL_DAYS:
        raise ArchiveError("backfill-days must be greater than 0 and at most 30")
    if reconcile_days is not None and not 0 < reconcile_days <= MAX_BACKFILL_DAYS:
        raise ArchiveError("reconcile-days must be greater than 0 and at most 30")

    documents = load_archive(root)
    generated_at = (now or utc_now()).astimezone(timezone.utc)
    limiter = recent.RateLimiter(request_interval)
    fetch_stats = recent.FetchStats()
    fetched: Dict[str, list[Dict[str, Any]]] = {}
    failures: Dict[str, str] = {}
    windows: Dict[str, Tuple[datetime, datetime]] = {}
    for icao in requested:
        if backfill_days is not None:
            hours = backfill_days * 24
        elif reconcile_days is not None:
            hours = reconcile_days * 24
        elif station_has_archive(icao, documents):
            hours = INCREMENTAL_OVERLAP_HOURS
        else:
            hours = INITIAL_BACKFILL_DAYS * 24
        windows[icao] = (generated_at - timedelta(hours=hours), generated_at)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(
                fetch_station,
                icao,
                windows[icao][0],
                windows[icao][1],
                limiter,
                fetch_stats,
                maximum_segment_hours,
            ): icao
            for icao in requested
        }
        for future in as_completed(futures):
            icao = futures[future]
            try:
                fetched[icao] = future.result()
                print(f"Fetched {icao}: {len(fetched[icao])} source records", file=sys.stderr)
            except Exception as error:
                failures[icao] = str(error)
                print(f"Failed {icao}: {error}", file=sys.stderr)

    changed: Dict[PartitionKey, Dict[str, Any]] = {}
    merge_totals = Counter()
    successful_stations = 0
    build_failures: Dict[str, str] = {}
    for icao in requested:
        if icao in failures:
            continue
        try:
            station_changes, station_stats = merge_station_records(
                icao, fetched[icao], {**documents, **changed}, generated_at
            )
        except ArchiveError as error:
            build_failures[icao] = str(error)
            continue
        changed.update(station_changes)
        merge_totals.update(station_stats)
        successful_stations += 1
    failures.update(build_failures)
    if successful_stations == 0:
        raise ArchiveError("All station updates failed; existing archive preserved")

    combined = {**documents, **changed}
    index = build_index(combined, generated_at)
    publish_transaction(root, documents, changed, index)
    valid, detail = validate_index(index, combined)
    if not valid:
        raise ArchiveError(f"Published index failed validation: {detail}")
    return {
        "generatedAt": iso_utc(generated_at),
        "requestedStations": len(requested),
        "successfulStations": successful_stations,
        "failedStations": len(failures),
        "failures": failures,
        "changedPartitions": len(changed),
        "recordCount": index["recordCount"],
        "stationCount": index["stationCount"],
        "earliestObsTime": index["earliestObsTime"],
        "latestObsTime": index["latestObsTime"],
        "exactDuplicates": merge_totals["exactDuplicates"],
        "correctionVariants": merge_totals["correctionVariants"],
        "newVariants": merge_totals["newVariants"],
        "requests": fetch_stats.requests,
        "retries": fetch_stats.retries,
        "splitSegments": fetch_stats.split_segments,
        "statuses": dict(fetch_stats.statuses),
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", help="Comma-separated current METAR ICAOs (default: all)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--backfill-days", type=float, help="Force a backfill window, maximum 30 days")
    mode.add_argument(
        "--reconcile-days",
        type=float,
        help=f"Force an overlapping reconciliation window (recommended {RECONCILIATION_DAYS:g})",
    )
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--request-interval", type=float, default=REQUEST_INTERVAL_SECONDS)
    parser.add_argument("--segment-hours", type=float, default=MAX_SEGMENT_HOURS)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    selected = None
    if args.ids:
        selected = [item.strip().upper() for item in args.ids.split(",") if item.strip()]
    try:
        with ArchiveLock():
            result = update_archive(
                selected_ids=selected,
                backfill_days=args.backfill_days,
                reconcile_days=args.reconcile_days,
                workers=args.workers,
                request_interval=args.request_interval,
                maximum_segment_hours=args.segment_hours,
            )
    except (ArchiveError, OSError, ValueError) as error:
        print(f"Archive update failed: {error}", file=sys.stderr)
        return 1
    print(f"Source: {recent.SOURCE_URL}")
    print(f"Generated at: {result['generatedAt']}")
    print(f"Requested stations: {result['requestedStations']}")
    print(f"Successful stations: {result['successfulStations']}")
    print(f"Failed stations: {result['failedStations']}")
    print(f"Changed UTC-month partitions: {result['changedPartitions']}")
    print(f"Archive stations: {result['stationCount']}")
    print(f"Archive observations: {result['recordCount']}")
    print(f"Earliest observation: {result['earliestObsTime']}")
    print(f"Latest observation: {result['latestObsTime']}")
    print(f"Exact duplicates suppressed: {result['exactDuplicates']}")
    print(f"Correction variants retained: {result['correctionVariants']}")
    print(f"New raw variants: {result['newVariants']}")
    print(f"HTTP requests: {result['requests']}")
    print("HTTP statuses: " + ", ".join(f"{key}={value}" for key, value in sorted(result["statuses"].items())))
    print(f"Retries: {result['retries']}")
    print(f"Segments split at {recent.MAX_RESULTS} records: {result['splitSegments']}")
    print("Failures: " + ("; ".join(f"{key}: {value}" for key, value in sorted(result["failures"].items())) or "none"))
    print(f"Output: {ARCHIVE_DIR}")
    return 0 if result["failedStations"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
