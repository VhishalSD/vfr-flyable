#!/usr/bin/env python3

import contextlib
import csv
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
    spec = importlib.util.spec_from_file_location("update_runways", SCRIPTS / "update-runways.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def csv_fixture(rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=sorted(UpdaterTests.updater.SOURCE_FIELDS))
    writer.writeheader()
    for row in rows:
        complete = {field: "" for field in writer.fieldnames}
        complete.update(row)
        writer.writerow(complete)
    return output.getvalue()


def complete_row(**overrides):
    row = {
        "id": "1", "airport_ident": "EBAW", "length_ft": "4951", "width_ft": "148",
        "surface": "ASP", "lighted": "1", "closed": "0", "le_ident": "11",
        "le_latitude_deg": "51.1878", "le_longitude_deg": "4.4731", "le_elevation_ft": "32",
        "le_heading_degT": "109", "le_displaced_threshold_ft": "200", "he_ident": "29",
        "he_latitude_deg": "51.1924", "he_longitude_deg": "4.4528", "he_elevation_ft": "39",
        "he_heading_degT": "289", "he_displaced_threshold_ft": "100",
    }
    row.update(overrides)
    return row


class UpdaterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.updater = load_updater_module()

    def test_full_schema_conversions_and_runway_pair(self):
        records, count = self.updater.parse_runways_csv(csv_fixture([complete_row()]), {"EBAW"})
        self.assertEqual(count, 1)
        record = records[0]
        self.assertEqual(record["runwayPair"], "11/29")
        self.assertEqual(record["length_m"], 1509.1)
        self.assertEqual(record["width_m"], 45.1)
        self.assertEqual(record["surface"], "ASP")
        self.assertTrue(record["lighted"])
        self.assertFalse(record["closed"])
        self.assertEqual(record["le_displaced_threshold_ft"], 200)
        self.assertEqual(record["he_heading_degT"], 289)
        self.assertIsNone(record["le_heading_degM"])
        self.assertIsNone(record["he_heading_degM"])

    def test_magnetic_headings_are_derived_without_overwriting_true(self):
        records, _ = self.updater.parse_runways_csv(
            csv_fixture([complete_row(le_heading_degT="109", he_heading_degT="289")]),
            {"EBAW"},
            {"EBAW": 2.8},
        )
        record = records[0]
        self.assertEqual((record["le_heading_degT"], record["he_heading_degT"]), (109, 289))
        self.assertEqual((record["le_heading_degM"], record["he_heading_degM"]), (106, 286))

    def test_missing_true_heading_has_missing_magnetic_heading(self):
        records, _ = self.updater.parse_runways_csv(
            csv_fixture([complete_row(le_heading_degT="")]), {"EBAW"}, {"EBAW": 2.8}
        )
        self.assertIsNone(records[0]["le_heading_degT"])
        self.assertIsNone(records[0]["le_heading_degM"])

    def test_missing_high_end_identifier_has_null_pair(self):
        records, _ = self.updater.parse_runways_csv(
            csv_fixture([complete_row(id="2", airport_ident="EHAM", he_ident="")]), {"EHAM"}
        )
        self.assertIsNone(records[0]["he_ident"])
        self.assertIsNone(records[0]["runwayPair"])

    def test_corrupt_source_is_rejected(self):
        with self.assertRaises(self.updater.UpdateError):
            self.updater.parse_runways_csv("id,airport_ident\n1,EBAW\n", {"EBAW"})
        with self.assertRaises(self.updater.UpdateError):
            self.updater.parse_runways_csv(
                csv_fixture([complete_row(le_latitude_deg="bad")]), {"EBAW"}
            )

    def test_network_failure_retries(self):
        with mock.patch.object(
            self.updater.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")
        ), mock.patch.object(self.updater.time, "sleep") as sleep:
            with self.assertRaises(self.updater.UpdateError):
                self.updater.download_runways_csv()
        self.assertEqual(sleep.call_count, self.updater.MAX_ATTEMPTS - 1)

    def test_failed_update_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            output = directory / "runways.json"
            fixture = directory / "bad.csv"
            original = (ROOT / "data" / "runways.json").read_text(encoding="utf-8")
            output.write_text(original, encoding="utf-8")
            fixture.write_text(csv_fixture([complete_row(length_ft="broken")]), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                status = self.updater.main(["--source-file", str(fixture), "--output", str(output)])
            self.assertEqual(status, 1)
            self.assertEqual(output.read_text(encoding="utf-8"), original)

    def test_main_network_failure_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "runways.json"
            original = (ROOT / "data" / "runways.json").read_text(encoding="utf-8")
            output.write_text(original, encoding="utf-8")
            with mock.patch.object(
                self.updater, "download_runways_csv", side_effect=self.updater.UpdateError("offline")
            ), contextlib.redirect_stderr(io.StringIO()):
                status = self.updater.main(["--output", str(output)])
            self.assertEqual(status, 1)
            self.assertEqual(output.read_text(encoding="utf-8"), original)

    def test_atomic_replace_failure_preserves_old_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "runways.json"
            output.write_text("old-valid-content", encoding="utf-8")
            with mock.patch.object(self.updater.os, "replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    self.updater.atomic_write_json(output, [{"safe": True}])
            self.assertEqual(output.read_text(encoding="utf-8"), "old-valid-content")

    def test_existing_corrupt_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            output = directory / "runways.json"
            fixture = directory / "valid.csv"
            output.write_text("{broken", encoding="utf-8")
            fixture.write_text(csv_fixture([complete_row()]), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                status = self.updater.main(["--source-file", str(fixture), "--output", str(output)])
            self.assertEqual(status, 1)
            self.assertEqual(output.read_text(encoding="utf-8"), "{broken")


if __name__ == "__main__":
    unittest.main()
