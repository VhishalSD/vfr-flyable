import copy
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from airport_geo import decimal_degrees_to_dms


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


METADATA_UPDATER = load_script("airport_metadata_integrity_test", "update-airport-metadata.py")
VALIDATOR = load_script("airport_backend_validator_test", "validate-airport-backend.py")


class DmsTests(unittest.TestCase):
    def test_positive_negative_and_zero_coordinates(self):
        self.assertEqual(decimal_degrees_to_dms(51.190667, "latitude"), "511126N")
        self.assertEqual(decimal_degrees_to_dms(-51.190667, "latitude"), "511126S")
        self.assertEqual(decimal_degrees_to_dms(4.463153, "longitude"), "0042747E")
        self.assertEqual(decimal_degrees_to_dms(-4.463153, "longitude"), "0042747W")
        self.assertEqual(decimal_degrees_to_dms(0, "latitude"), "000000N")
        self.assertEqual(decimal_degrees_to_dms(0, "longitude"), "0000000E")

    def test_rounding_carries_seconds_and_minutes(self):
        self.assertEqual(decimal_degrees_to_dms(12.9998611112, "latitude"), "130000N")
        self.assertEqual(decimal_degrees_to_dms(-179.9998611112, "longitude"), "1800000W")


class DaylightOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.airports = METADATA_UPDATER.load_airports()
        cls.original = json.loads((ROOT / "data" / "airport-metadata.json").read_text())

    def test_refresh_changes_no_static_airport_metadata_fir_or_timezone(self):
        moment = datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)
        refreshed, stats = METADATA_UPDATER.build_daylight_only_document(
            self.original, self.airports, moment
        )
        METADATA_UPDATER.validate_dataset(refreshed, self.airports)
        self.assertEqual(sum(stats["counts"].values()), 618)
        for icao, original_record in self.original["airports"].items():
            new_record = refreshed["airports"][icao]
            original_static = {key: value for key, value in original_record.items() if key != "daylight"}
            new_static = {key: value for key, value in new_record.items() if key != "daylight"}
            self.assertEqual(new_static, original_static)
            self.assertEqual(new_record["fir"], original_record["fir"])
            self.assertEqual(new_record["timezone"], original_record["timezone"])
        for source_name, source in self.original["sources"].items():
            if source_name != "daylight":
                self.assertEqual(refreshed["sources"][source_name], source)

    def test_cli_daylight_only_does_not_call_static_downloads(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "airport-metadata.json"
            shutil.copy2(ROOT / "data" / "airport-metadata.json", output)
            patches = [
                mock.patch.object(METADATA_UPDATER, name, side_effect=AssertionError("download called"))
                for name in (
                    "download_fir_geojson", "download_airports_csv",
                    "download_countries_csv", "download_regions_csv",
                )
            ]
            for patcher in patches:
                patcher.start()
            try:
                result = METADATA_UPDATER.main([
                    "--daylight-only", "--output", str(output),
                    "--now", "2026-09-02T01:00:00Z",
                ])
            finally:
                for patcher in reversed(patches):
                    patcher.stop()
            self.assertEqual(result, 0)
            document = json.loads(output.read_text())
            self.assertEqual(document["airports"]["EBAW"]["daylight"]["date"], "2026-09-02")


class BackendValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.metadata = VALIDATOR.load_json(ROOT / "data" / "airport-metadata.json")
        cls.runways = VALIDATOR.load_json(ROOT / "data" / "runways.json")
        cls.nearby = VALIDATOR.load_json(ROOT / "data" / "airport-nearby.json")
        cls.navaids = VALIDATOR.load_json(ROOT / "data" / "airport-navaids.json")

    def test_current_generated_datasets_pass(self):
        report = VALIDATOR.validate_backend(
            self.metadata, self.runways, self.nearby, self.navaids
        )
        self.assertEqual(report["metadataRecordCount"], 618)
        self.assertEqual(report["runwayRecordCount"], len(self.runways))

    def test_missing_metadata_is_detected(self):
        metadata = copy.deepcopy(self.metadata)
        del metadata["airports"]["EBAW"]
        with self.assertRaises(VALIDATOR.ValidationError):
            VALIDATOR.validate_backend(metadata, self.runways, self.nearby, self.navaids)

    def test_invalid_bearing_is_detected(self):
        nearby = copy.deepcopy(self.nearby)
        nearby["airports"]["EBAW"]["nearby"][0]["bearingDeg"] = 360
        with self.assertRaises(VALIDATOR.ValidationError):
            VALIDATOR.validate_backend(self.metadata, self.runways, nearby, self.navaids)

    def test_duplicate_nearby_and_navaid_ids_are_detected(self):
        for original, list_field, root_label in (
            (self.nearby, "nearby", "nearby"),
            (self.navaids, "navaids", "navaid"),
        ):
            with self.subTest(root=root_label):
                document = copy.deepcopy(original)
                group = document["airports"]["EBAW"]
                group[list_field].append(copy.deepcopy(group[list_field][0]))
                group["recordCount"] += 1
                document["recordCount"] += 1
                args = (document, self.navaids) if root_label == "nearby" else (self.nearby, document)
                with self.assertRaises(VALIDATOR.ValidationError):
                    VALIDATOR.validate_backend(self.metadata, self.runways, *args)

    def test_magnetic_heading_without_true_heading_is_detected(self):
        runways = copy.deepcopy(self.runways)
        record = next(row for row in runways if row["le_heading_degM"] is not None)
        record["le_heading_degT"] = None
        with self.assertRaises(VALIDATOR.ValidationError):
            VALIDATOR.validate_backend(self.metadata, runways, self.nearby, self.navaids)


if __name__ == "__main__":
    unittest.main()
