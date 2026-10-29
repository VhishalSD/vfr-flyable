#!/usr/bin/env python3

import importlib.util
import json
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from airport_time import (  # noqa: E402
    calculate_daylight,
    is_dst,
    utc_offset_minutes,
    utc_to_local,
)


def load_updater_module():
    spec = importlib.util.spec_from_file_location(
        "update_airport_metadata", SCRIPTS / "update-airport-metadata.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AirportTimeTests(unittest.TestCase):
    def test_brussels_summer_and_winter_dst(self):
        summer = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
        winter = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        self.assertEqual(utc_offset_minutes(summer, "Europe/Brussels"), 120)
        self.assertTrue(is_dst(summer, "Europe/Brussels"))
        self.assertEqual(utc_offset_minutes(winter, "Europe/Brussels"), 60)
        self.assertFalse(is_dst(winter, "Europe/Brussels"))

    def test_brussels_dst_transition(self):
        before = datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc)
        after = datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)
        self.assertEqual(utc_to_local(before, "Europe/Brussels").hour, 1)
        self.assertEqual(utc_offset_minutes(before, "Europe/Brussels"), 60)
        self.assertEqual(utc_to_local(after, "Europe/Brussels").hour, 3)
        self.assertEqual(utc_offset_minutes(after, "Europe/Brussels"), 120)

    def test_daylight_across_locations(self):
        locations = (
            (51.1907, 4.4632, "Europe/Brussels"),
            (51.4700, -0.4543, "Europe/London"),
            (48.3538, 11.7861, "Europe/Berlin"),
        )
        for latitude, longitude, timezone_name in locations:
            with self.subTest(timezone=timezone_name):
                result = calculate_daylight(latitude, longitude, timezone_name, date(2026, 8, 17))
                self.assertEqual(result["state"], "normal")
                self.assertGreater(result["dayLengthSeconds"], 0)

    def test_polar_day_and_night_do_not_raise(self):
        summer = calculate_daylight(69.6492, 18.9553, "Europe/Oslo", date(2026, 6, 21))
        winter = calculate_daylight(69.6492, 18.9553, "Europe/Oslo", date(2026, 12, 21))
        self.assertEqual((summer["state"], summer["dayLengthSeconds"]), ("polarDay", 86400))
        self.assertEqual((winter["state"], winter["dayLengthSeconds"]), ("polarNight", 0))


class DatasetValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.updater = load_updater_module()

    def test_local_airport_source_has_expected_shape(self):
        airports = self.updater.load_airports()
        self.assertEqual(len(airports), 618)
        self.assertEqual(len({item["icao"] for item in airports}), 618)

    def test_generated_dataset_and_named_airports(self):
        airports = self.updater.load_airports()
        with (ROOT / "data" / "airport-metadata.json").open(encoding="utf-8") as handle:
            document = json.load(handle)
        self.updater.validate_dataset(document, airports)
        expected_firs = {
            "EBAW": "Brussels FIR",
            "EHAM": "Amsterdam FIR",
            "EHRD": "Amsterdam FIR",
            "EGLL": "London FIR",
            "LFPG": "Paris FIR",
            "EDDM": "Munchen FIR",
        }
        for icao, expected_fir in expected_firs.items():
            with self.subTest(icao=icao):
                record = document["airports"][icao]
                self.assertIsNotNone(record["municipality"])
                self.assertIsNotNone(record["timezone"])
                self.assertEqual(record["fir"], expected_fir)
                self.assertEqual(record["daylight"]["state"], "normal")
                for field in (
                    "name", "type", "isoCountry", "countryName", "isoRegion", "regionName",
                    "scheduledService", "gpsCode", "ourAirportsId", "sourceCoordinates",
                    "signedDeclinationDeg", "magneticVariationDeg", "magneticVariationDirection",
                    "magneticVariationYear", "magneticVariationDate", "magneticVariationSource",
                ):
                    self.assertIn(field, record)
                self.assertIsNotNone(record["name"])
                self.assertIsNotNone(record["type"])
                self.assertIsInstance(record["scheduledService"], bool)
                self.assertIsInstance(record["ourAirportsId"], int)
                self.assertIsNotNone(record["sourceCoordinates"]["latitude"])
                self.assertIsNotNone(record["sourceCoordinates"]["longitude"])
                self.assertEqual(record["magneticVariationSource"], "WMM-2025")
                self.assertEqual(record["magneticVariationYear"], 2026)
        self.assertTrue(any(record["iata"] is None for record in document["airports"].values()))

    def test_static_source_mapping_and_null_preservation(self):
        row = {
            "id": "2152", "name": "Antwerp International Airport (Deurne)",
            "type": "medium_airport", "iso_country": "BE", "iso_region": "BE-VAN",
            "scheduled_service": "yes", "gps_code": "EBAW", "local_code": "",
            "latitude_deg": "51.1894", "longitude_deg": "4.4603", "municipality": "Antwerp",
            "iata_code": "AN", "elevation_ft": "",
        }
        countries = {"BE": {"name": "Belgium"}}
        regions = {"BE-VAN": {"name": "Flanders"}}
        metadata = self.updater.source_metadata(row, countries, regions)
        self.assertEqual(metadata["name"], "Antwerp International Airport (Deurne)")
        self.assertEqual(metadata["type"], "medium_airport")
        self.assertEqual(metadata["countryName"], "Belgium")
        self.assertEqual(metadata["regionName"], "Flanders")
        self.assertTrue(metadata["scheduledService"])
        self.assertEqual(metadata["gpsCode"], "EBAW")
        self.assertIsNone(metadata["localCode"])
        self.assertIsNone(metadata["iata"])
        preserved = self.updater.preserve_existing_source_values(
            metadata, {"iata": "ANR", "elevationFt": 39, "elevationM": 11.8872}
        )
        self.assertEqual(preserved["iata"], "ANR")
        self.assertEqual(preserved["elevationFt"], 39)


if __name__ == "__main__":
    unittest.main()
