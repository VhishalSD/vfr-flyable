#!/usr/bin/env python3

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_updater_module():
    spec = importlib.util.spec_from_file_location(
        "update_airport_metadata_fir", SCRIPTS / "update-airport-metadata.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rectangle_feature(designator, name, west, south, east, north):
    return {
        "type": "Feature",
        "properties": {"designator": designator, "type": "FIR", "name": name},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [west, south], [east, south], [east, north], [west, north], [west, south]
            ]],
        },
    }


def valid_fixture_document():
    return {
        "type": "FeatureCollection",
        "features": [
            rectangle_feature("EBBU", "Brussels FIR", 2, 49, 7, 52),
            rectangle_feature("EHAA", "Amsterdam FIR", 3, 52.01, 8, 54),
            rectangle_feature("EGTT", "London FIR", -2, 49, 1, 52),
            rectangle_feature("LFFF", "Paris FIR", 1.1, 48, 4, 51),
            rectangle_feature("EDMM", "Munchen FIR", 8, 47, 14, 50),
        ],
    }


class FirSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.updater = load_updater_module()

    def test_coordinate_matching_and_outside_known_firs(self):
        features = self.updater.parse_fir_boundaries(json.dumps(valid_fixture_document()))
        self.assertEqual(
            self.updater.find_fir(4.46, 51.19, features),
            ("Brussels FIR", "EBBU", None),
        )
        fir, designator, warning = self.updater.find_fir(100, 0, features)
        self.assertIsNone(fir)
        self.assertIsNone(designator)
        self.assertEqual(warning, "no containing FIR polygon")

    def test_invalid_and_incomplete_sources_are_rejected(self):
        with self.assertRaises(self.updater.UpdateError):
            self.updater.parse_fir_boundaries("not json")
        incomplete = valid_fixture_document()
        incomplete["features"] = incomplete["features"][:-1]
        with self.assertRaises(self.updater.UpdateError):
            self.updater.parse_fir_boundaries(json.dumps(incomplete))

    def test_missing_or_corrupt_source_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            output = directory / "airport-metadata.json"
            original = (ROOT / "data" / "airport-metadata.json").read_text(encoding="utf-8")
            output.write_text(original, encoding="utf-8")

            missing = directory / "missing.geojson"
            with contextlib.redirect_stderr(io.StringIO()):
                status = self.updater.main(
                    ["--fir-source-file", str(missing), "--output", str(output)]
                )
            self.assertEqual(status, 1)
            self.assertEqual(output.read_text(encoding="utf-8"), original)

            corrupt = directory / "corrupt.geojson"
            corrupt.write_text("{}", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                status = self.updater.main(
                    ["--fir-source-file", str(corrupt), "--output", str(output)]
                )
            self.assertEqual(status, 1)
            self.assertEqual(output.read_text(encoding="utf-8"), original)

    def test_existing_metadata_fields_remain_present(self):
        document = json.loads((ROOT / "data" / "airport-metadata.json").read_text())
        record = document["airports"]["EBAW"]
        self.assertEqual(record["municipality"], "Antwerp")
        self.assertEqual(record["iata"], "ANR")
        self.assertEqual(record["elevationFt"], 39)
        self.assertEqual(record["elevationM"], 11.8872)
        self.assertEqual(record["timezone"], "Europe/Brussels")
        self.assertIsInstance(record["daylight"], dict)


if __name__ == "__main__":
    unittest.main()
