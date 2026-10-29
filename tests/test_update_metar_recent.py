#!/usr/bin/env python3

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "update-metar-recent.py"


def load_updater():
    spec = importlib.util.spec_from_file_location("update_metar_recent", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_record(icao="EBAW", obs_time=1788029400, **values):
    record = {
        "icaoId": icao,
        "receiptTime": "2026-08-29T18:56:16.102Z",
        "obsTime": obs_time,
        "reportTime": "2026-08-29T19:00:00.000Z",
        "temp": 20,
        "dewp": 17,
        "wdir": 190,
        "wspd": 12,
        "visib": "6+",
        "altim": 1006,
        "metarType": "METAR",
        "rawOb": "METAR EBAW 291850Z 19012KT 9999 BKN016 20/17 Q1006",
        "clouds": [{"cover": "BKN", "base": 1600}],
        "fltCat": "MVFR",
    }
    record.update(values)
    return record


class MetarRecentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.u = load_updater()
        cls.now = datetime(2026, 8, 29, 19, 5, 49, tzinfo=timezone.utc)

    def build(self, records, zone="Europe/Brussels", existing=None):
        return self.u.build_station_document(
            "EBAW", zone, records, generated_at=self.now, existing_document=existing
        )[0]

    def test_seven_local_calendar_days_and_dst_boundaries(self):
        start, end, first, last = self.u.local_calendar_window(
            "Europe/Brussels", self.now
        )
        self.assertEqual(first.isoformat(), "2026-08-23")
        self.assertEqual(last.isoformat(), "2026-08-29")
        self.assertEqual(start.isoformat(), "2026-08-22T22:00:00+00:00")
        self.assertEqual(end, self.now)

        summer = self.u.local_calendar_window(
            "Europe/Brussels", datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
        )[0]
        winter = self.u.local_calendar_window(
            "Europe/Brussels", datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        )[0]
        self.assertEqual(summer.hour, 22)
        self.assertEqual(winter.hour, 23)

    def test_metar_speci_raw_category_weather_visibility_cloud_ceiling_and_gust(self):
        metar = source_record(
            wxString="-RA",
            wgst=25,
            rawOb="METAR EBAW 291850Z 19012G25KT 9999 -RA BKN016 20/17 Q1006",
        )
        speci = source_record(
            obs_time=1788030000,
            metarType="SPECI",
            fltCat="IFR",
            visib=1.25,
            wxString="TSRA",
            rawOb="SPECI EBAW 291900Z 19012G25KT 2000 TSRA FEW008 BKN009CB 20/17 Q1006",
            clouds=[{"cover": "FEW", "base": 800}, {"cover": "BKN", "base": 900}],
        )
        document = self.build([metar, speci])
        first, second = document["observations"]
        self.assertEqual(first["rawOb"], metar["rawOb"])
        self.assertEqual(first["flightCategory"], "MVFR")
        self.assertEqual(first["flightCategorySource"], "AWC")
        self.assertEqual(first["weatherRaw"], "-RA")
        self.assertEqual(first["dewPointC"], 17)
        self.assertEqual(first["dewPointSource"], "AWC")
        self.assertEqual(first["visib"], 6)
        self.assertEqual(first["visibilityQualifier"], "greaterThan")
        self.assertEqual(first["sourceVisibility"], "9999")
        self.assertEqual(first["cloudLayers"][0]["baseFtAgl"], 1600)
        self.assertEqual(first["ceilingFt"], 1600)
        self.assertEqual(first["ceilingSource"], "cloudLayer")
        self.assertEqual(first["windGust"], 25)
        self.assertEqual(second["metarType"], "SPECI")
        self.assertEqual(second["ceilingFt"], 900)

    def test_dew_point_mapping_and_strict_raw_fallback_parser(self):
        _, normalized, _ = self.u.normalize_record(source_record(dewp=-5), "EBAW")
        self.assertEqual(normalized["dewPointC"], -5)
        self.assertEqual(normalized["dewPointSource"], "AWC")
        cases = {
            "METAR EBAW 291850Z 19012KT 9999 BKN016 17/13 Q1006": 13,
            "METAR EBAW 291850Z 19012KT 9999 BKN016 M02/M05 Q1006": -5,
            "METAR EBAW 291850Z 19012KT 9999 BKN016 03/M01 Q1006": -1,
            "METAR EBAW 291850Z 19012KT 9999 BKN016 18// Q1006": None,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(self.u.parse_dew_point_from_raw(raw), expected)
        self.assertIsNone(self.u.parse_dew_point_from_raw("METAR EBAW 291850Z 10/20SM Q1006"))
        self.assertIsNone(self.u.parse_dew_point_from_raw("METAR EBAW 291850Z 17/13 18/14 Q1006"))

    def test_vertical_visibility_vrb_and_directional_range(self):
        vertical = source_record(
            vertVis=200,
            wdir="VRB",
            clouds=[],
            fltCat=None,
            visib=.5,
            rawOb="METAR EBAW 291850Z VRB03KT 0800 FG VV002 16/15 Q1006",
        )
        observed_at, normalized, _ = self.u.normalize_record(vertical, "EBAW")
        self.assertIsNotNone(observed_at)
        self.assertEqual(normalized["verticalVisibilityFt"], 200)
        self.assertEqual(normalized["ceilingFt"], 200)
        self.assertEqual(normalized["ceilingSource"], "verticalVisibility")
        self.assertEqual(normalized["windDirection"], "VRB")
        self.assertEqual(normalized["variableWind"]["type"], "VRB")
        self.assertEqual(normalized["flightCategory"], "LIFR")
        self.assertEqual(normalized["flightCategorySource"], "derived")

        ranged = source_record(
            rawOb="METAR EBAW 291850Z 35007KT 310V010 9999 FEW040 20/17 Q1006"
        )
        _, normalized_range, _ = self.u.normalize_record(ranged, "EBAW")
        self.assertEqual(
            normalized_range["variableWind"],
            {"type": "range", "from": 310, "to": 10, "raw": "310V010"},
        )

    def test_duplicate_timestamp_chooses_complete_latest_record(self):
        incomplete = source_record(wxString=None, wgst=None)
        complete = source_record(
            receiptTime="2026-08-29T19:01:00Z", wxString="RA", wgst=24
        )
        document = self.build([complete, incomplete])
        self.assertEqual(document["recordCount"], 1)
        self.assertEqual(document["observations"][0]["weatherRaw"], "RA")
        self.assertEqual(document["observations"][0]["windGust"], 24)

    def test_retention_excludes_previous_local_date_and_multiple_icao_is_rejected(self):
        expired = source_record(obs_time="2026-08-22T21:59:59Z")
        current = source_record()
        document = self.build([expired, current])
        self.assertEqual(document["recordCount"], 1)
        with self.assertRaises(self.u.UpdateError):
            self.build([source_record(icao="EHAM")])

    def test_400_result_window_is_split(self):
        calls = []

        def fetcher(icao, start, end):
            calls.append((start, end))
            return [{}] * 400 if len(calls) == 1 else []

        stats = self.u.FetchStats()
        result = self.u.fetch_window_segmented(
            "EBAW",
            datetime(2026, 8, 28, tzinfo=timezone.utc),
            datetime(2026, 8, 29, tzinfo=timezone.utc),
            fetcher,
            stats,
        )
        self.assertEqual(result, [])
        self.assertEqual(len(calls), 3)
        self.assertEqual(stats.split_segments, 1)

    def test_corrupt_existing_file_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "metar-recent"
            output.mkdir()
            corrupt = output / "EBAW.json"
            corrupt.write_bytes(b"{broken")
            original = corrupt.read_bytes()
            with self.assertRaises(self.u.UpdateError):
                self.u.load_existing_documents(output)
            self.assertEqual(corrupt.read_bytes(), original)

    def test_transaction_replace_failure_restores_all_old_files(self):
        old_document = self.build([source_record()])
        new_document = self.build(
            [source_record(temp=21, receiptTime="2026-08-29T19:02:00Z")]
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "metar-recent"
            output.mkdir()
            self.u.write_document(output / "EBAW.json", old_document)
            original = (output / "EBAW.json").read_bytes()
            real_replace = os.replace
            calls = 0

            def failing_replace(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated commit failure")
                return real_replace(source, destination)

            with mock.patch.object(self.u.os, "replace", side_effect=failing_replace):
                with self.assertRaises(OSError):
                    self.u.commit_documents_transactionally(
                        {"EBAW": new_document}, output
                    )
            self.assertEqual((output / "EBAW.json").read_bytes(), original)

    def test_network_or_corrupt_response_cannot_reach_transaction(self):
        with mock.patch.object(
            self.u, "fetch_segment", side_effect=self.u.UpdateError("offline")
        ), mock.patch.object(self.u, "commit_documents_transactionally") as commit:
            limiter = self.u.RateLimiter(0)
            with self.assertRaises(self.u.UpdateError):
                self.u.fetch_station(
                    "EBAW", "Europe/Brussels", self.now, limiter, self.u.FetchStats(), 168
                )
            commit.assert_not_called()

        with self.assertRaises(self.u.UpdateError):
            self.u.normalize_record({"icaoId": "EBAW", "obsTime": 1}, "EBAW")

    def test_network_failure_preserves_existing_station_content(self):
        existing = self.build([source_record()])
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            live = directory / "metar-live.json"
            metadata = directory / "airport-metadata.json"
            output = directory / "metar-recent"
            live.write_text(json.dumps([{"icaoId": "EBAW"}]))
            metadata.write_text(
                json.dumps(
                    {"airports": {"EBAW": {"timezone": "Europe/Brussels"}}}
                )
            )
            output.mkdir()
            self.u.write_document(output / "EBAW.json", existing)
            original = (output / "EBAW.json").read_bytes()
            with mock.patch.object(self.u, "LIVE_PATH", live), mock.patch.object(
                self.u, "METADATA_PATH", metadata
            ), mock.patch.object(self.u, "OUTPUT_DIR", output), mock.patch.object(
                self.u, "fetch_station", side_effect=self.u.UpdateError("offline")
            ):
                stats = self.u.update(selected_ids=["EBAW"], workers=1)
            self.assertEqual(stats["preservedFailedStations"], 1)
            self.assertEqual((output / "EBAW.json").read_bytes(), original)


class GeneratedRecentDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.u = load_updater()

    def test_generated_reference_airports_when_dataset_exists(self):
        output = ROOT / "data" / "metar-recent"
        if not output.exists():
            self.skipTest("recent dataset has not been generated yet")
        for icao in ("EBAW", "EHAM", "EGLL"):
            document = json.loads((output / f"{icao}.json").read_text())
            valid, detail = self.u.validate_document(document, icao)
            self.assertTrue(valid, detail)
            self.assertGreater(document["recordCount"], 0)


if __name__ == "__main__":
    unittest.main()
