#!/usr/bin/env python3

import importlib.util
import json
import math
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "update-metar-trends.py"


def load_updater():
    spec = importlib.util.spec_from_file_location("update_metar_trends", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def observation(moment, **values):
    record = {
        "obsTime": moment.isoformat().replace("+00:00", "Z"),
        "reportTime": None,
        "receiptTime": None,
        "metarType": "METAR",
        "flightCategory": "VFR",
        "flightCategorySource": "AWC",
        "weatherRaw": None,
        "temp": 20,
        "dewPointC": 10,
        "dewPointSource": "AWC",
        "visib": 6,
        "visibilityQualifier": "greaterThan",
        "sourceVisibility": "9999",
        "cloudLayers": [{"cover": "SCT", "baseFtAgl": 4000, "topFtAgl": None}],
        "ceilingFt": None,
        "ceilingSource": None,
        "verticalVisibilityFt": None,
        "windDirection": 180,
        "variableWind": None,
        "windSpeed": 10,
        "windGust": 35,
        "altimeterHpa": 1015,
        "rawOb": "METAR EBAW 010000Z 18010G35KT 9999 SCT040 20/10 Q1015",
    }
    record.update(values)
    return record


def full_day(start, **values):
    return [observation(start + timedelta(hours=hour), **values) for hour in range(24)]


class MetarTrendsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.u = load_updater()
        cls.generated = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)

    def test_visibility_conversion_and_thresholds(self):
        self.assertAlmostEqual(self.u.VFR_VISIBILITY_SM, 4.9709695, places=6)
        self.assertAlmostEqual(self.u.MVFR_VISIBILITY_SM, 3.106856, places=6)
        base = observation(datetime(2026, 8, 1, tzinfo=timezone.utc), visib=self.u.VFR_VISIBILITY_SM, visibilityQualifier="exact", ceilingFt=3000, cloudLayers=[{"cover": "BKN", "baseFtAgl": 3000}])
        self.assertTrue(self.u.condition_result(base, self.u.VFR_VISIBILITY_SM, 3000))
        base["visib"] -= 0.001
        self.assertFalse(self.u.condition_result(base, self.u.VFR_VISIBILITY_SM, 3000))
        base["visib"] = self.u.MVFR_VISIBILITY_SM
        base["ceilingFt"] = 1000
        self.assertTrue(self.u.condition_result(base, self.u.MVFR_VISIBILITY_SM, 1000))

    def test_unlimited_and_unknown_ceiling(self):
        clear = observation(datetime(2026, 8, 1, tzinfo=timezone.utc), cloudLayers=[], rawOb="METAR EBAW 010000Z 18010KT CAVOK 20/10 Q1015")
        self.assertEqual(self.u.ceiling_value(clear), (math.inf, True))
        unknown = dict(clear, rawOb="METAR EBAW 010000Z 18010KT 9999 // VV/// 20/10 Q1015")
        self.assertEqual(self.u.ceiling_value(unknown), (None, False))
        self.assertIsNone(self.u.condition_result(unknown, self.u.VFR_VISIBILITY_SM, 3000))

    def test_local_date_utc_midnight_and_dst(self):
        summer = datetime(2026, 8, 31, 22, 30, tzinfo=timezone.utc)
        winter = datetime(2026, 12, 31, 23, 30, tzinfo=timezone.utc)
        zone = self.u.ZoneInfo("Europe/Brussels")
        self.assertEqual(summer.astimezone(zone).date().isoformat(), "2026-09-01")
        self.assertEqual(winter.astimezone(zone).date().isoformat(), "2027-01-01")
        self.assertEqual(summer.astimezone(zone).utcoffset(), timedelta(hours=2))
        self.assertEqual(winter.astimezone(zone).utcoffset(), timedelta(hours=1))

    def test_daily_extrema_rh_and_sustained_wind_excludes_gust(self):
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        records = full_day(start)
        records[0]["temp"] = 10
        records[-1]["temp"] = 30
        records[0]["dewPointC"] = 5
        for item in records:
            item["windSpeed"] = 8
            item["windGust"] = 99
        doc = self.u.aggregate_station("EBAW", "UTC", records, self.generated)
        month = doc["years"]["2026"]["8"]
        self.assertEqual(month["maxTemperatureC"], 30)
        self.assertEqual(month["minTemperatureC"], 10)
        self.assertEqual(month["windSpeedKt"], 8)
        self.assertGreater(month["relativeHumidityPercent"], 0)
        expected = self.u.relative_humidity_percent(20, 10)
        self.assertAlmostEqual(expected, 52.5, places=1)

    def test_speci_is_retained_observation_based(self):
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        records = full_day(start)
        speci = observation(start + timedelta(minutes=30), metarType="SPECI", visib=1, visibilityQualifier="exact", ceilingFt=400, cloudLayers=[{"cover": "BKN", "baseFtAgl": 400}])
        records.append(speci)
        doc = self.u.aggregate_station("EBAW", "UTC", records, self.generated)
        month = doc["years"]["2026"]["8"]
        self.assertEqual(month["quality"]["observationCount"], 25)
        self.assertEqual(month["vfrPercent"], 96)

    def test_raw_dewpoint_fallback_and_missing_fields(self):
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        records = full_day(start, dewPointC=None, dewPointSource=None, rawOb="METAR EBAW 010000Z 18010KT 9999 SCT040 M02/M05 Q1015")
        doc = self.u.aggregate_station("EBAW", "UTC", records, self.generated)
        quality = doc["years"]["2026"]["8"]["quality"]
        self.assertEqual(quality["dewPointRawFallbackCount"], 24)
        missing = full_day(start, temp=None, dewPointC=None, rawOb="METAR EBAW 010000Z 18010KT 9999 SCT040 18// Q1015", windSpeed=None)
        month = self.u.aggregate_station("EBAW", "UTC", missing, self.generated)["years"]["2026"]["8"]
        self.assertIsNone(month["relativeHumidityPercent"])
        self.assertIsNone(month["windSpeedKt"])
        self.assertIsNone(month["maxTemperatureC"])

    def test_partial_month_and_no_climate_average(self):
        records = full_day(datetime(2026, 8, 1, tzinfo=timezone.utc))
        doc = self.u.aggregate_station("EBAW", "UTC", records, self.generated)
        self.assertTrue(doc["years"]["2026"]["8"]["partialMonth"])
        self.assertEqual(doc["averages"], {})

    def test_historical_full_month_and_two_year_average_rule(self):
        records = []
        for year in (2024, 2025):
            for day in range(1, 32):
                records.extend(full_day(datetime(year, 1, day, tzinfo=timezone.utc)))
        doc = self.u.aggregate_station("EBAW", "UTC", records, self.generated)
        self.assertFalse(doc["years"]["2024"]["1"]["partialMonth"])
        self.assertFalse(doc["years"]["2025"]["1"]["partialMonth"])
        self.assertEqual(doc["averages"]["1"]["completeYearCount"], 2)

    def test_no_nan_and_deterministic_output(self):
        records = full_day(datetime(2026, 8, 1, tzinfo=timezone.utc))
        first = self.u.aggregate_station("EBAW", "UTC", records, self.generated)
        second = self.u.aggregate_station("EBAW", "UTC", records, self.generated)
        self.assertEqual(first, second)
        encoded = json.dumps(first, allow_nan=False, sort_keys=True)
        self.assertNotIn("NaN", encoded)

    def test_atomic_output_failure_preserves_existing(self):
        records = full_day(datetime(2026, 8, 1, tzinfo=timezone.utc))
        document = self.u.aggregate_station("EBAW", "UTC", records, self.generated)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "EBAW.json"
            target.write_bytes(b"existing-valid-sentinel")
            before = target.read_bytes()
            with self.assertRaises(OSError):
                self.u.publish_file(target, document, replace=mock.Mock(side_effect=OSError("disk failure")))
            self.assertEqual(target.read_bytes(), before)

    def test_atomic_all_station_swap_failure_restores_previous_directory(self):
        records = full_day(datetime(2026, 8, 1, tzinfo=timezone.utc))
        document = self.u.aggregate_station("EBAW", "UTC", records, self.generated)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "metar-trends"
            output.mkdir()
            sentinel = output / "old.txt"
            sentinel.write_text("preserve me")
            real_replace = os.replace
            calls = 0

            def fail_second(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated publish failure")
                return real_replace(source, destination)

            with mock.patch.object(self.u.os, "replace", side_effect=fail_second):
                with self.assertRaises(OSError):
                    self.u.publish_all({"EBAW": document}, output)
            self.assertEqual(sentinel.read_text(), "preserve me")


if __name__ == "__main__":
    unittest.main()
