#!/usr/bin/env python3

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_updater_module():
    spec = importlib.util.spec_from_file_location("update_taf", SCRIPTS / "update-taf.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_record(
    icao="EHAM",
    *,
    raw_taf=None,
    issue_time="2026-08-29T11:14:00Z",
    most_recent=1,
):
    return {
        "icaoId": icao,
        "rawTAF": raw_taf or f"TAF {icao} 291114Z 2912/3018 21014KT 9999 SCT025",
        "issueTime": issue_time,
        "bulletinTime": "2026-08-29T11:00:00Z",
        "dbPopTime": "2026-08-29T11:25:17Z",
        "validTimeFrom": 1788004800,
        "validTimeTo": 1788112800,
        "mostRecent": most_recent,
        "remarks": "",
        "prior": 0,
        "lat": 52.315,
        "lon": 4.79,
        "elev": -2,
        "name": "Test airport",
        "fcsts": [
            {
                "timeFrom": 1788004800,
                "timeTo": 1788112800,
                "timeBec": None,
                "fcstChange": None,
                "probability": None,
                "wdir": 210,
                "wspd": 14,
                "wgst": None,
                "visib": "6+",
                "altim": None,
                "vertVis": None,
                "wxString": None,
                "notDecoded": None,
                "clouds": [{"cover": "SCT", "base": 2500, "top": None}],
                "icgTurb": [],
                "temp": [],
            }
        ],
    }


class TafUpdaterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.updater = load_updater_module()

    def build_valid_document(self):
        document, errors = self.updater.build_document(
            ["EBAW", "EHAM"],
            [source_record()],
            "2026-08-29T12:00:00Z",
        )
        self.assertEqual(errors, [])
        return document

    def test_airport_with_taf_and_airport_without_taf(self):
        document = self.build_valid_document()
        self.assertEqual(document["recordCount"], 1)
        self.assertEqual(document["availableStationCount"], 1)
        self.assertIn("EHAM", document["airports"])
        self.assertNotIn("EBAW", document["airports"])
        valid, detail = self.updater.validate_document(
            document, allowed_ids={"EBAW", "EHAM"}
        )
        self.assertTrue(valid, detail)

    def test_duplicate_source_icao_is_deduplicated_deterministically(self):
        older = source_record(
            raw_taf="TAF EHAM 290500Z 2906/3012 18005KT CAVOK",
            issue_time="2026-08-29T05:00:00Z",
            most_recent=0,
        )
        latest = source_record()
        document, _ = self.updater.build_document(
            ["EHAM"], [older, latest], "2026-08-29T12:00:00Z"
        )
        self.assertEqual(document["recordCount"], 1)
        self.assertEqual(document["airports"]["EHAM"]["rawTAF"], latest["rawTAF"])

    def test_invalid_or_unrequested_source_data_is_rejected(self):
        invalid = source_record()
        invalid["rawTAF"] = ""
        with self.assertRaises(self.updater.UpdateError):
            self.updater.build_document(
                ["EHAM"], [invalid], "2026-08-29T12:00:00Z"
            )
        with self.assertRaises(self.updater.UpdateError):
            self.updater.build_document(
                ["EBAW"], [source_record()], "2026-08-29T12:00:00Z"
            )

    def test_airport_source_rejects_duplicate_icao(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "airports.json"
            path.write_text(json.dumps([{"icao": "EHAM"}, {"icao": "EHAM"}]))
            with self.assertRaises(self.updater.UpdateError):
                self.updater.load_station_ids(path)

    def test_network_failure_retries_and_raises(self):
        with mock.patch.object(
            self.updater.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("offline"),
        ) as urlopen, mock.patch.object(self.updater.time, "sleep"):
            with self.assertRaises(self.updater.UpdateError):
                self.updater.fetch_batch(["EHAM"], 1, [])
        self.assertEqual(urlopen.call_count, self.updater.MAX_ATTEMPTS)

    def test_failed_main_preserves_existing_valid_file(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            airports_path = directory / "airports.json"
            output_path = directory / "taf-live.json"
            airports_path.write_text(json.dumps([{"icao": "EHAM"}]))
            original_document = self.build_valid_document()
            self.updater.atomic_write(original_document, output_path)
            original_payload = output_path.read_bytes()
            with mock.patch.object(self.updater, "AIRPORTS_PATH", airports_path), mock.patch.object(
                self.updater, "OUTPUT_PATH", output_path
            ), mock.patch.object(
                self.updater, "fetch_batch", side_effect=self.updater.UpdateError("offline")
            ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                status = self.updater.main()
            self.assertEqual(status, 1)
            self.assertEqual(output_path.read_bytes(), original_payload)

    def test_invalid_api_payload_preserves_existing_valid_file(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            airports_path = directory / "airports.json"
            output_path = directory / "taf-live.json"
            airports_path.write_text(json.dumps([{"icao": "EHAM"}]))
            original_document = self.build_valid_document()
            self.updater.atomic_write(original_document, output_path)
            original_payload = output_path.read_bytes()
            invalid = source_record()
            invalid["rawTAF"] = ""
            with mock.patch.object(self.updater, "AIRPORTS_PATH", airports_path), mock.patch.object(
                self.updater, "OUTPUT_PATH", output_path
            ), mock.patch.object(
                self.updater, "fetch_batch", return_value=([invalid], 200, 0)
            ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                status = self.updater.main()
            self.assertEqual(status, 1)
            self.assertEqual(output_path.read_bytes(), original_payload)

    def test_atomic_write_and_replace_failure(self):
        document = self.build_valid_document()
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            output = directory / "taf-live.json"
            self.updater.atomic_write(document, output)
            self.assertEqual(json.loads(output.read_text()), document)
            self.assertEqual(list(directory.glob(".taf-live-*.json")), [])
            original = output.read_bytes()
            with mock.patch.object(self.updater.os, "replace", side_effect=OSError("blocked")):
                with self.assertRaises(OSError):
                    self.updater.atomic_write({**document, "generatedAt": "2026-08-29T13:00:00Z"}, output)
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(list(directory.glob(".taf-live-*.json")), [])

    def test_schema_validation_rejects_inconsistent_counts_and_times(self):
        document = self.build_valid_document()
        broken_count = {**document, "recordCount": 2}
        self.assertFalse(self.updater.validate_document(broken_count)[0])
        broken_time = json.loads(json.dumps(document))
        broken_time["airports"]["EHAM"]["validTo"] = "invalid"
        self.assertFalse(self.updater.validate_document(broken_time)[0])


class GeneratedTafDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.updater = load_updater_module()

    def test_generated_dataset_and_reference_airports(self):
        document = json.loads((ROOT / "data" / "taf-live.json").read_text())
        allowed = set(self.updater.load_station_ids())
        valid, detail = self.updater.validate_document(document, allowed_ids=allowed)
        self.assertTrue(valid, detail)
        for icao in ("EHAM", "EGLL", "LFPG", "EDDM", "EBAW"):
            with self.subTest(icao=icao):
                station = document["airports"].get(icao)
                self.assertIsNotNone(station)
                self.assertTrue(station["rawTAF"].startswith("TAF "))


if __name__ == "__main__":
    unittest.main()
