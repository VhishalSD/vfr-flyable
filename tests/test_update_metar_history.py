#!/usr/bin/env python3

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_updater_module():
    spec = importlib.util.spec_from_file_location(
        "update_metar_history", SCRIPTS / "update-metar-history.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_record(icao, obs_time, **values):
    record = {
        "icaoId": icao,
        "obsTime": obs_time,
        "temp": 12,
        "visib": 6,
        "wspd": 8,
        "wgst": None,
        "altim": 1016,
    }
    record.update(values)
    return record


class MetarHistoryUpdaterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.updater = load_updater_module()
        cls.now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)

    def document(self, ids, records):
        return self.updater.build_document(
            ids, records, generated_at=self.now
        )[0]

    def test_merge_adds_new_observation_without_duplicating_identical_one(self):
        existing = self.document(
            ["EBAW"], [source_record("EBAW", "2026-08-29T10:00:00Z")]
        )
        document, stats = self.updater.build_document(
            ["EBAW"],
            [
                source_record("EBAW", "2026-08-29T10:00:00Z"),
                source_record("EBAW", "2026-08-29T11:00:00Z", temp=13),
            ],
            existing_document=existing,
            generated_at=self.now,
        )
        observations = document["airports"]["EBAW"]["observations"]
        self.assertEqual([item["obsTime"] for item in observations], [
            "2026-08-29T10:00:00Z", "2026-08-29T11:00:00Z"
        ])
        self.assertEqual(stats["sourceNewObservations"], 1)
        self.assertEqual(stats["sourceUpdatedObservations"], 1)

    def test_older_observation_is_sorted_and_retention_removes_only_expired_data(self):
        existing = self.document(
            ["EBAW"],
            [
                source_record("EBAW", "2026-08-28T12:00:00Z"),
                source_record("EBAW", "2026-08-29T10:00:00Z"),
            ],
        )
        document, stats = self.updater.build_document(
            ["EBAW"],
            [
                source_record("EBAW", "2026-08-28T11:59:59Z"),
                source_record("EBAW", "2026-08-29T09:00:00Z"),
                source_record("EBAW", "2026-08-29T11:00:00Z"),
            ],
            existing_document=existing,
            generated_at=self.now,
        )
        times = [item["obsTime"] for item in document["airports"]["EBAW"]["observations"]]
        self.assertEqual(times, [
            "2026-08-28T12:00:00Z",
            "2026-08-29T09:00:00Z",
            "2026-08-29T10:00:00Z",
            "2026-08-29T11:00:00Z",
        ])
        self.assertEqual(stats["ignoredOutsideWindow"], 1)

    def test_multiple_airports_are_separate_and_unknown_airports_are_ignored(self):
        document, stats = self.updater.build_document(
            ["EBAW", "EHAM", "EGLL"],
            [
                source_record("EGLL", "2026-08-29T10:00:00Z"),
                source_record("EBAW", "2026-08-29T09:00:00Z"),
                source_record("EHAM", "2026-08-29T11:00:00Z"),
                source_record("KJFK", "2026-08-29T11:00:00Z"),
            ],
            generated_at=self.now,
        )
        self.assertEqual(set(document["airports"]), {"EBAW", "EHAM", "EGLL"})
        self.assertEqual(
            {icao: len(value["observations"]) for icao, value in document["airports"].items()},
            {"EBAW": 1, "EHAM": 1, "EGLL": 1},
        )
        self.assertEqual(stats["ignoredUnknownStation"], 1)

    def test_validation_rejects_duplicates_and_non_numeric_measurements(self):
        document = self.document(
            ["EBAW"], [source_record("EBAW", "2026-08-29T10:00:00Z")]
        )
        duplicate = json.loads(json.dumps(document))
        duplicate["airports"]["EBAW"]["observations"].append(
            dict(duplicate["airports"]["EBAW"]["observations"][0])
        )
        self.assertFalse(self.updater.validate_document(duplicate)[0])
        invalid_number = json.loads(json.dumps(document))
        invalid_number["airports"]["EBAW"]["observations"][0]["temp"] = "12"
        self.assertFalse(self.updater.validate_document(invalid_number)[0])

    def test_missing_new_data_or_network_failure_preserves_existing_file(self):
        for result in ([], self.updater.UpdateError("offline")):
            with self.subTest(result=type(result).__name__), tempfile.TemporaryDirectory() as directory:
                directory = Path(directory)
                live_path = directory / "metar-live.json"
                output_path = directory / "metar-history.json"
                live_path.write_text(json.dumps([{"icaoId": "EBAW"}]))
                existing = self.document(
                    ["EBAW"], [source_record("EBAW", "2026-08-29T10:00:00Z")]
                )
                self.updater.write_json_atomic(existing, output_path)
                original = output_path.read_bytes()
                side_effect = result if isinstance(result, Exception) else None
                return_value = (result, []) if side_effect is None else None
                with mock.patch.object(self.updater, "LIVE_PATH", live_path), mock.patch.object(
                    self.updater, "OUTPUT_PATH", output_path
                ), mock.patch.object(
                    self.updater, "fetch_batch", side_effect=side_effect, return_value=return_value
                ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(self.updater.main(), 1)
                self.assertEqual(output_path.read_bytes(), original)

    def test_corrupt_existing_history_is_reported_and_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            live_path = directory / "metar-live.json"
            output_path = directory / "metar-history.json"
            live_path.write_text(json.dumps([{"icaoId": "EBAW"}]))
            output_path.write_bytes(b"{broken")
            original = output_path.read_bytes()
            with mock.patch.object(self.updater, "LIVE_PATH", live_path), mock.patch.object(
                self.updater, "OUTPUT_PATH", output_path
            ), mock.patch.object(self.updater, "fetch_batch") as fetch, contextlib.redirect_stdout(
                io.StringIO()
            ), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.updater.main(), 1)
            fetch.assert_not_called()
            self.assertEqual(output_path.read_bytes(), original)

    def test_corrupt_live_input_preserves_existing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            live_path = directory / "metar-live.json"
            output_path = directory / "metar-history.json"
            live_path.write_bytes(b"{broken")
            existing = self.document(
                ["EBAW"], [source_record("EBAW", "2026-08-29T10:00:00Z")]
            )
            self.updater.write_json_atomic(existing, output_path)
            original = output_path.read_bytes()
            with mock.patch.object(self.updater, "LIVE_PATH", live_path), mock.patch.object(
                self.updater, "OUTPUT_PATH", output_path
            ), mock.patch.object(self.updater, "fetch_batch") as fetch, contextlib.redirect_stdout(
                io.StringIO()
            ), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.updater.main(), 1)
            fetch.assert_not_called()
            self.assertEqual(output_path.read_bytes(), original)

    def test_atomic_replace_failure_keeps_previous_file(self):
        document = self.document(
            ["EBAW"], [source_record("EBAW", "2026-08-29T10:00:00Z")]
        )
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "metar-history.json"
            self.updater.write_json_atomic(document, output_path)
            original = output_path.read_bytes()
            updated = self.document(
                ["EBAW"], [source_record("EBAW", "2026-08-29T11:00:00Z")]
            )
            with mock.patch.object(self.updater.os, "replace", side_effect=OSError("blocked")):
                with self.assertRaises(OSError):
                    self.updater.write_json_atomic(updated, output_path)
            self.assertEqual(output_path.read_bytes(), original)
            self.assertEqual(list(Path(directory).glob(".metar-history-*.json")), [])


class GeneratedMetarHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.updater = load_updater_module()

    def test_generated_dataset_and_reference_airports(self):
        document = json.loads((ROOT / "data" / "metar-history.json").read_text())
        expected = {
            record["icaoId"]
            for record in json.loads((ROOT / "data" / "metar-live.json").read_text())
        }
        valid, detail = self.updater.validate_document(document, expected_ids=expected)
        self.assertTrue(valid, detail)
        for icao in ("EBAW", "EHAM", "EGLL"):
            self.assertTrue(document["airports"][icao]["observations"])


if __name__ == "__main__":
    unittest.main()
