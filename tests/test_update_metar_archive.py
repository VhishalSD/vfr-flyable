#!/usr/bin/env python3

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "update-metar-archive.py"


def load_updater():
    spec = importlib.util.spec_from_file_location("update_metar_archive", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_record(icao="EBAW", obs_time="2026-08-29T18:50:00Z", **values):
    record = {
        "icaoId": icao,
        "receiptTime": "2026-08-29T18:56:00Z",
        "obsTime": obs_time,
        "reportTime": obs_time,
        "temp": 20,
        "wdir": 190,
        "wspd": 12,
        "wgst": None,
        "visib": 6,
        "altim": 1006,
        "metarType": "METAR",
        "rawOb": f"METAR {icao} 291850Z 19012KT 9999 BKN016 20/17 Q1006",
        "clouds": [{"cover": "BKN", "base": 1600}],
        "fltCat": "MVFR",
    }
    record.update(values)
    return record


class MetarArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.u = load_updater()
        cls.now = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)

    def normalized(self, source):
        return self.u.recent.normalize_record(source, source["icaoId"])[1]

    def test_initial_backfill_partitions_two_months_and_preserves_speci(self):
        records = [
            source_record(obs_time="2026-07-31T23:50:00Z"),
            source_record(
                obs_time="2026-08-01T00:20:00Z",
                metarType="SPECI",
                rawOb="SPECI EBAW 010020Z 19012KT 9999 BKN016 20/17 Q1006",
            ),
        ]
        changed, stats = self.u.merge_station_records("EBAW", records, {}, self.now)
        self.assertEqual(set(changed), {("EBAW", 2026, 7), ("EBAW", 2026, 8)})
        self.assertEqual(changed[("EBAW", 2026, 8)]["observations"][0]["metarType"], "SPECI")
        self.assertEqual(stats["newVariants"], 2)

    def test_exact_duplicate_and_corrected_raw_are_deduplicated_with_revision(self):
        original = self.normalized(source_record())
        duplicate = dict(original)
        correction_source = source_record(
            receiptTime="2026-08-29T19:02:00Z",
            temp=21,
            rawOb="METAR EBAW 291850Z COR 19012KT 9999 BKN016 21/17 Q1006",
        )
        correction = self.normalized(correction_source)
        merged, stats = self.u.merge_observations([], [original, duplicate, correction])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["rawOb"], correction["rawOb"])
        self.assertEqual(merged[0]["revisions"][0]["rawOb"], original["rawOb"])
        self.assertEqual(stats["exactDuplicates"], 1)
        self.assertEqual(stats["correctionVariants"], 1)

    def test_missing_receipt_uses_deterministic_completeness_fallback(self):
        first = self.normalized(source_record(receiptTime=None, wxString=None))
        second = self.normalized(
            source_record(
                receiptTime=None,
                wxString="RA",
                rawOb="METAR EBAW 291850Z 19012KT 9999 RA BKN016 20/17 Q1006",
            )
        )
        merged, _ = self.u.merge_observations([], [first, second])
        self.assertEqual(merged[0]["weatherRaw"], "RA")

    def test_late_record_extends_existing_month_without_removing_old_records(self):
        old = self.normalized(source_record(obs_time="2026-08-01T00:20:00Z"))
        existing = self.u.new_partition("EBAW", 2026, 8, [old], self.now)
        late_source = source_record(
            obs_time="2026-08-15T10:20:00Z",
            receiptTime="2026-08-15T10:25:00Z",
            rawOb="SPECI EBAW 151020Z 18008KT 5000 RA BKN012 18/17 Q1008",
            metarType="SPECI",
        )
        changed, _ = self.u.merge_station_records(
            "EBAW", [late_source], {("EBAW", 2026, 8): existing}, self.now
        )
        observations = changed[("EBAW", 2026, 8)]["observations"]
        self.assertEqual(len(observations), 2)
        self.assertEqual(observations[0]["obsTime"], old["obsTime"])
        self.assertEqual(observations[1]["metarType"], "SPECI")

    def test_partition_and_index_validation(self):
        observation = self.normalized(source_record())
        partition = self.u.new_partition("EBAW", 2026, 8, [observation], self.now)
        partitions = {("EBAW", 2026, 8): partition}
        index = self.u.build_index(partitions, self.now)
        self.assertTrue(self.u.validate_partition(partition, "EBAW", 2026, 8)[0])
        self.assertTrue(self.u.validate_index(index, partitions)[0])
        self.assertEqual(index["stations"]["EBAW"]["years"], {"2026": ["08"]})

    def test_corrupt_existing_partition_and_index_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "archive"
            part = root / "EBAW" / "2026" / "08.json"
            part.parent.mkdir(parents=True)
            part.write_bytes(b"{broken")
            original = part.read_bytes()
            with self.assertRaises(self.u.ArchiveError):
                self.u.load_archive(root)
            self.assertEqual(part.read_bytes(), original)

            part.unlink()
            root.joinpath("index.json").write_bytes(b"{broken")
            original_index = root.joinpath("index.json").read_bytes()
            with self.assertRaises(self.u.ArchiveError):
                self.u.load_archive(root)
            self.assertEqual(root.joinpath("index.json").read_bytes(), original_index)

    def test_atomic_partition_or_index_failure_rolls_back_everything(self):
        old_observation = self.normalized(source_record())
        old_partition = self.u.new_partition("EBAW", 2026, 8, [old_observation], self.now)
        old_documents = {("EBAW", 2026, 8): old_partition}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "archive"
            self.u.publish_transaction(
                root, {}, old_documents, self.u.build_index(old_documents, self.now)
            )
            partition_path = root / "EBAW" / "2026" / "08.json"
            index_path = root / "index.json"
            old_partition_bytes = partition_path.read_bytes()
            old_index_bytes = index_path.read_bytes()

            new_observation = self.normalized(
                source_record(
                    obs_time="2026-08-29T19:50:00Z",
                    receiptTime="2026-08-29T19:56:00Z",
                    rawOb="METAR EBAW 291950Z 19012KT 9999 BKN016 20/17 Q1006",
                )
            )
            merged, _ = self.u.merge_observations([old_observation], [new_observation])
            new_partition = self.u.new_partition("EBAW", 2026, 8, merged, self.now)
            changed = {("EBAW", 2026, 8): new_partition}
            combined = {**old_documents, **changed}
            calls = 0

            def fail_on_index(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated index replace failure")
                return os.replace(source, destination)

            with self.assertRaises(OSError):
                self.u.publish_transaction(
                    root,
                    old_documents,
                    changed,
                    self.u.build_index(combined, self.now + timedelta(minutes=1)),
                    replace=fail_on_index,
                )
            self.assertEqual(partition_path.read_bytes(), old_partition_bytes)
            self.assertEqual(index_path.read_bytes(), old_index_bytes)
            loaded = self.u.load_archive(root)
            self.assertEqual(loaded[("EBAW", 2026, 8)]["recordCount"], 1)

    def test_network_and_corrupt_api_failure_preserve_existing_archive(self):
        for mode in ("network", "corrupt"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                directory = Path(directory)
                live = directory / "metar-live.json"
                live.write_text(json.dumps([{"icaoId": "EBAW"}]))
                root = directory / "archive"
                root.mkdir()
                before = list(root.rglob("*"))

                def failing_fetch(*args, **kwargs):
                    if mode == "network":
                        raise self.u.ArchiveError("offline")
                    return [{"icaoId": "EBAW", "obsTime": "bad"}]

                with mock.patch.object(self.u, "LIVE_PATH", live):
                    with self.assertRaises(self.u.ArchiveError):
                        self.u.update_archive(
                            selected_ids=["EBAW"], root=root, workers=1, fetch_station=failing_fetch
                        )
                self.assertEqual(list(root.rglob("*")), before)

    def test_partial_station_failure_publishes_only_success_and_consistent_index(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            live = directory / "metar-live.json"
            live.write_text(json.dumps([{"icaoId": "EBAW"}, {"icaoId": "EHAM"}]))
            root = directory / "archive"

            def fetch(icao, *args):
                if icao == "EHAM":
                    raise self.u.ArchiveError("offline")
                return [source_record(icao=icao)]

            with mock.patch.object(self.u, "LIVE_PATH", live):
                result = self.u.update_archive(
                    root=root, workers=2, request_interval=0, fetch_station=fetch, now=self.now
                )
            self.assertEqual(result["successfulStations"], 1)
            self.assertEqual(result["failedStations"], 1)
            documents = self.u.load_archive(root)
            self.assertEqual(set(key[0] for key in documents), {"EBAW"})
            index = json.loads((root / "index.json").read_text())
            self.assertTrue(self.u.validate_index(index, documents)[0])

    def test_lock_blocks_second_writer_and_recovers_dead_stale_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".lock"
            first = self.u.ArchiveLock(path)
            first.acquire()
            try:
                with self.assertRaises(self.u.ArchiveError):
                    self.u.ArchiveLock(path).acquire()
            finally:
                first.release()
            stale = {
                "schemaVersion": 1,
                "pid": 99999999,
                "hostname": self.u.socket.gethostname(),
                "startedAt": "2026-01-01T00:00:00Z",
                "token": "dead",
            }
            path.write_text(json.dumps(stale))
            recovered = self.u.ArchiveLock(path)
            recovered.acquire()
            self.assertTrue(recovered.acquired)
            recovered.release()
            self.assertFalse(path.exists())

    def test_400_record_segment_is_split(self):
        calls = []

        def fetcher(icao, start, end):
            calls.append((start, end))
            return [{}] * 400 if len(calls) == 1 else []

        stats = self.u.recent.FetchStats()
        result = self.u.recent.fetch_window_segmented(
            "EBAW",
            datetime(2026, 8, 1, tzinfo=timezone.utc),
            datetime(2026, 8, 2, tzinfo=timezone.utc),
            fetcher,
            stats,
        )
        self.assertEqual(result, [])
        self.assertEqual(len(calls), 3)
        self.assertEqual(stats.split_segments, 1)


if __name__ == "__main__":
    unittest.main()
