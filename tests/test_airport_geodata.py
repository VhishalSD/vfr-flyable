import csv
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import airport_geo
import ourairports_backend


def load_script(module_name, filename):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NEARBY = load_script("update_nearby_airports", "update-nearby-airports.py")
NAVAIDS = load_script("update_navaids", "update-navaids.py")
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def csv_text(fieldnames, rows):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def airport_row(source_id, ident, location_type, latitude, longitude, **overrides):
    row = {
        "id": str(source_id),
        "ident": ident,
        "type": location_type,
        "name": f"{ident} location",
        "latitude_deg": str(latitude),
        "longitude_deg": str(longitude),
        "elevation_ft": "100",
        "iso_country": "BE",
        "iso_region": "BE-VAN",
        "municipality": "Antwerp",
        "scheduled_service": "no",
        "gps_code": ident if len(ident) == 4 else "",
        "local_code": "",
    }
    row.update(overrides)
    return row


def navaid_row(source_id, ident, navaid_type, latitude, longitude, **overrides):
    row = {
        "id": str(source_id),
        "ident": ident,
        "name": f"{ident} navaid",
        "type": navaid_type,
        "frequency_khz": "113500",
        "latitude_deg": str(latitude),
        "longitude_deg": str(longitude),
        "elevation_ft": "50",
        "iso_country": "BE",
        "associated_airport": "EBAW",
    }
    row.update(overrides)
    return row


ORIGINS = {
    "EBAW": {
        "icaoId": "EBAW",
        "ourAirportsId": 1,
        "latitude": 51.1894,
        "longitude": 4.4603,
    }
}


class GeoHelperTests(unittest.TestCase):
    def test_cardinal_bearings(self):
        self.assertEqual(airport_geo.round_bearing_deg(airport_geo.initial_bearing_deg(0, 0, 1, 0)), 0)
        self.assertEqual(airport_geo.round_bearing_deg(airport_geo.initial_bearing_deg(0, 0, 0, 1)), 90)
        self.assertEqual(airport_geo.round_bearing_deg(airport_geo.initial_bearing_deg(0, 0, -1, 0)), 180)
        self.assertEqual(airport_geo.round_bearing_deg(airport_geo.initial_bearing_deg(0, 0, 0, -1)), 270)

    def test_wrap_identical_and_negative_longitude(self):
        self.assertEqual(airport_geo.round_bearing_deg(airport_geo.initial_bearing_deg(0, 179, 0, -179)), 90)
        self.assertEqual(airport_geo.distance_and_bearing(10, -5, 10, -5), (0.0, 0))
        self.assertGreater(airport_geo.haversine_km(10, -5, 10, -4), 100)


class NearbyAirportTests(unittest.TestCase):
    def source(self):
        rows = [
            airport_row(1, "EBAW", "medium_airport", 51.1894, 4.4603),
            airport_row(2, "EBH1", "heliport", 51.20, 4.46),
            airport_row(3, "CLSD", "closed", 51.21, 4.46),
            airport_row(4, "NOMET", "small_airport", 51.22, 4.46, gps_code=""),
            airport_row(5, "FAR1", "large_airport", 53.0, 4.46),
            airport_row(2, "EBH1", "heliport", 51.20, 4.46),
        ]
        return csv_text(sorted(NEARBY.SOURCE_FIELDS), rows)

    def test_selection_types_dedupe_sort_radius_and_maximum(self):
        locations, count, duplicates = NEARBY.parse_airports_csv(self.source())
        self.assertEqual((count, duplicates), (6, 1))
        document = NEARBY.build_document(ORIGINS, locations, NOW, NOW, radius_km=10, max_results=2)
        records = document["airports"]["EBAW"]["nearby"]
        self.assertEqual([record["ourAirportsId"] for record in records], [2, 3])
        self.assertEqual(records[0]["type"], "heliport")
        self.assertTrue(records[1]["closed"])
        self.assertNotIn(1, [record["ourAirportsId"] for record in records])
        self.assertLessEqual(records[0]["distanceKm"], records[1]["distanceKm"])
        self.assertTrue(all(record["distanceKm"] <= 10 for record in records))
        larger = NEARBY.build_document(ORIGINS, locations, NOW, NOW, radius_km=10, max_results=10)
        self.assertIn(4, [record["ourAirportsId"] for record in larger["airports"]["EBAW"]["nearby"]])

    def test_corrupt_source_rejected(self):
        with self.assertRaises(ourairports_backend.UpdateError):
            NEARBY.parse_airports_csv("id,ident\n1,EBAW\n")


class NavaidTests(unittest.TestCase):
    def source(self):
        rows = [
            navaid_row(10, "ANT", "VOR-DME", 51.19, 4.47),
            navaid_row(11, "ONW", "NDB", 51.25, 4.48),
            navaid_row(12, "FAR", "VOR", 55.0, 4.46),
            navaid_row(13, "MISS", "DME", "", ""),
            navaid_row(10, "ANT", "VOR-DME", 51.19, 4.47),
        ]
        return csv_text(sorted(NAVAIDS.SOURCE_FIELDS), rows)

    def test_types_missing_coordinates_dedupe_sort_radius_and_maximum(self):
        records, count, duplicates, missing = NAVAIDS.parse_navaids_csv(self.source())
        self.assertEqual((count, duplicates, missing), (5, 1, 1))
        document = NAVAIDS.build_document(ORIGINS, records, NOW, NOW, radius_km=20, max_results=1)
        selected = document["airports"]["EBAW"]["navaids"]
        self.assertEqual(len(selected), 1)
        self.assertEqual((selected[0]["ident"], selected[0]["type"]), ("ANT", "VOR-DME"))
        larger = NAVAIDS.build_document(ORIGINS, records, NOW, NOW, radius_km=20, max_results=10)
        types = {record["type"] for record in larger["airports"]["EBAW"]["navaids"]}
        self.assertEqual(types, {"VOR-DME", "NDB"})

    def test_corrupt_source_rejected(self):
        with self.assertRaises(ourairports_backend.UpdateError):
            NAVAIDS.parse_navaids_csv("id,ident\n1,ANT\n")


class PublicationSafetyTests(unittest.TestCase):
    def test_network_failure_is_retried_and_reported(self):
        with mock.patch.object(ourairports_backend.urllib.request, "urlopen", side_effect=OSError("offline")), mock.patch.object(ourairports_backend.time, "sleep") as sleep:
            with self.assertRaises(ourairports_backend.UpdateError):
                ourairports_backend.download_csv("https://invalid.example/data.csv", "fixture", ["id"])
        self.assertEqual(sleep.call_count, ourairports_backend.MAX_ATTEMPTS - 1)

    def test_atomic_replace_failure_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.json"
            output.write_text('{"safe":true}\n', encoding="utf-8")
            with mock.patch.object(ourairports_backend.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    ourairports_backend.atomic_write_json(output, {"safe": False})
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"safe": True})
            self.assertEqual(list(output.parent.glob(".output.json.*.tmp")), [])

    def test_corrupt_existing_output_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nearby.json"
            output.write_text("{broken", encoding="utf-8")
            with self.assertRaises(ourairports_backend.UpdateError):
                NEARBY.load_existing(output, ORIGINS)
            with self.assertRaises(ourairports_backend.UpdateError):
                NAVAIDS.load_existing(output, ORIGINS)

    def test_updater_download_failure_never_reaches_publication(self):
        for updater in (NEARBY, NAVAIDS):
            with self.subTest(updater=updater.__name__), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "existing.json"
                output.write_text('{"safe":true}\n', encoding="utf-8")
                with mock.patch.object(updater, "load_origins", return_value=ORIGINS), mock.patch.object(
                    updater, "load_existing", return_value={"recordCount": 1}
                ), mock.patch.object(
                    updater, "download_csv", side_effect=ourairports_backend.UpdateError("offline")
                ), mock.patch.object(updater, "atomic_write_json") as publish:
                    self.assertEqual(updater.main(["--output", str(output)]), 1)
                publish.assert_not_called()
                self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"safe": True})


class GeneratedDatasetTests(unittest.TestCase):
    def test_complete_generated_outputs_and_reference_airports(self):
        origins = ourairports_backend.load_origins(ROOT / "data" / "airport-metadata.json")
        nearby = json.loads((ROOT / "data" / "airport-nearby.json").read_text(encoding="utf-8"))
        navaids = json.loads((ROOT / "data" / "airport-navaids.json").read_text(encoding="utf-8"))
        NEARBY.validate_document(nearby, origins)
        NAVAIDS.validate_document(navaids, origins)
        for icao in ("EBAW", "EHAM", "EGLL", "LFPG", "EDDM"):
            self.assertIn(icao, nearby["airports"])
            self.assertIn(icao, navaids["airports"])
            self.assertGreater(nearby["airports"][icao]["recordCount"], 0)
            self.assertGreater(navaids["airports"][icao]["recordCount"], 0)
        ebaw_idents = {record["ident"] for record in navaids["airports"]["EBAW"]["navaids"]}
        self.assertTrue({"ANT", "ONW", "NIK"}.issubset(ebaw_idents))


if __name__ == "__main__":
    unittest.main()
