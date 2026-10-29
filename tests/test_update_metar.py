#!/usr/bin/env python3

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "update-metar.py"


def load_updater():
    spec = importlib.util.spec_from_file_location("update_metar", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def airport(icao="EBAW", country="Belgium", lat=51.1907, lon=4.4632):
    return {"icao": icao, "country": country, "lat": lat, "lon": lon}


def source_metar(
    icao="EBAW",
    *,
    observation_time="2026-08-31T08:20:00Z",
    raw=None,
    temperature="19.0",
):
    raw = raw or f"METAR {icao} 310820Z 35004KT CAVOK 19/13 Q1016 NOSIG"
    return f"""
      <METAR>
        <raw_text>{raw}</raw_text>
        <station_id>{icao}</station_id>
        <observation_time>{observation_time}</observation_time>
        <latitude>51.1907</latitude>
        <longitude>4.4632</longitude>
        <temp_c>{temperature}</temp_c>
        <dewpoint_c>13.0</dewpoint_c>
        <wind_dir_degrees>350</wind_dir_degrees>
        <wind_speed_kt>4</wind_speed_kt>
        <visibility_statute_mi>6.0</visibility_statute_mi>
        <altim_in_hg>30.00</altim_in_hg>
        <flight_category>VFR</flight_category>
        <sky_condition sky_cover="SCT" cloud_base_ft_agl="4000" />
      </METAR>
    """


def source_document(*records):
    return ("<response><data>" + "".join(records) + "</data></response>").encode()


def live_record(icao="EBAW", obs_time="2026-08-31T08:20:00Z"):
    return {
        "icaoId": icao,
        "obsTime": obs_time,
        "rawOb": f"METAR {icao} 310820Z 35004KT CAVOK 19/13 Q1016 NOSIG",
        "fltCat": "VFR",
        "visib": 6,
        "wdir": 350,
        "wspd": 4,
        "wgst": None,
        "altim": 1016,
        "clouds": [{"cover": "SCT", "base": 4000}],
        "ceiling": None,
        "temp": 19,
        "dewp": 13,
        "wxString": None,
        "lat": 51.1907,
        "lon": 4.4632,
    }


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def getcode(self):
        return self.status

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class MetarUpdaterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.u = load_updater()

    def run_main(self, directory, airports, payload):
        airports_path = directory / "airports.json"
        output_path = directory / "metar-live.json"
        airports_path.write_text(json.dumps(airports))
        with mock.patch.object(self.u, "AIRPORTS_PATH", airports_path), mock.patch.object(
            self.u, "OUTPUT_PATH", output_path
        ), mock.patch.object(
            self.u, "download_cache", return_value=payload
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            status = self.u.main()
        return status, output_path

    def test_normal_valid_update_and_obs_time_mapping(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            status, output = self.run_main(
                directory, [airport()], source_document(source_metar())
            )
            self.assertEqual(status, 0)
            document = json.loads(output.read_text())
            self.assertEqual(document[0]["icaoId"], "EBAW")
            self.assertEqual(document[0]["obsTime"], "2026-08-31T08:20:00Z")
            self.assertTrue(self.u.validate_dataset(document)[0])

    def test_temporary_network_failure_retries_then_succeeds(self):
        payload = source_document(source_metar())
        with mock.patch.object(
            self.u.urllib.request,
            "urlopen",
            side_effect=[urllib.error.URLError("temporary"), FakeResponse(payload)],
        ) as urlopen, mock.patch.object(self.u.time, "sleep") as sleep:
            self.assertEqual(self.u.download_cache(), payload)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_http_429_retry_after_is_respected(self):
        payload = source_document(source_metar())
        limited = urllib.error.HTTPError(
            self.u.SOURCE_URL, 429, "rate limited", {"Retry-After": "7"}, None
        )
        with mock.patch.object(
            self.u.urllib.request,
            "urlopen",
            side_effect=[limited, FakeResponse(payload)],
        ) as urlopen, mock.patch.object(self.u.time, "sleep") as sleep:
            self.assertEqual(self.u.download_cache(), payload)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(7.0)

    def test_terminal_network_failure_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            airports_path = directory / "airports.json"
            output = directory / "metar-live.json"
            airports_path.write_text(json.dumps([airport()]))
            self.u.write_json([live_record()], output)
            original = output.read_bytes()
            with mock.patch.object(self.u, "AIRPORTS_PATH", airports_path), mock.patch.object(
                self.u, "OUTPUT_PATH", output
            ), mock.patch.object(
                self.u.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("offline"),
            ) as urlopen, mock.patch.object(
                self.u.time, "sleep"
            ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                status = self.u.main()
            self.assertEqual(status, 1)
            self.assertEqual(urlopen.call_count, self.u.MAX_ATTEMPTS)
            self.assertEqual(output.read_bytes(), original)

    def test_corrupt_xml_and_corrupt_airport_json_preserve_existing_file(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            airports_path = directory / "airports.json"
            output = directory / "metar-live.json"
            airports_path.write_text(json.dumps([airport()]))
            self.u.write_json([live_record()], output)
            original = output.read_bytes()
            for airport_payload, source_payload in (
                (json.dumps([airport()]), b"not XML or JSON"),
                ("{broken JSON", source_document(source_metar())),
            ):
                airports_path.write_text(airport_payload)
                with mock.patch.object(self.u, "AIRPORTS_PATH", airports_path), mock.patch.object(
                    self.u, "OUTPUT_PATH", output
                ), mock.patch.object(
                    self.u, "download_cache", return_value=source_payload
                ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(self.u.main(), 1)
                self.assertEqual(output.read_bytes(), original)

    def test_empty_source_does_not_replace_valid_snapshot(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            output = directory / "metar-live.json"
            self.u.write_json([live_record()], output)
            original = output.read_bytes()
            airports_path = directory / "airports.json"
            airports_path.write_text(json.dumps([airport()]))
            with mock.patch.object(self.u, "AIRPORTS_PATH", airports_path), mock.patch.object(
                self.u, "OUTPUT_PATH", output
            ), mock.patch.object(
                self.u, "download_cache", return_value=b"<response><data /></response>"
            ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.u.main(), 1)
            self.assertEqual(output.read_bytes(), original)

    def test_eighty_percent_drop_guard_rejects_strong_drop(self):
        codes = [f"EBA{letter}" for letter in "ABCDEFGHIJ"]
        previous = [live_record(code) for code in codes]
        incoming = source_document(*(source_metar(code) for code in codes[:7]))
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            output = directory / "metar-live.json"
            self.u.write_json(previous, output)
            original = output.read_bytes()
            status, _ = self.run_main(
                directory, [airport(code) for code in codes], incoming
            )
            self.assertEqual(status, 1)
            self.assertEqual(output.read_bytes(), original)

    def test_first_run_without_existing_file_accepts_valid_nonempty_snapshot(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            status, output = self.run_main(
                directory, [airport()], source_document(source_metar())
            )
            self.assertEqual(status, 0)
            self.assertTrue(output.exists())

    def test_corrupt_existing_file_can_be_replaced_by_fully_valid_snapshot(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            output = directory / "metar-live.json"
            output.write_text("{corrupt")
            status, _ = self.run_main(
                directory, [airport()], source_document(source_metar())
            )
            self.assertEqual(status, 0)
            self.assertTrue(self.u.validate_dataset(json.loads(output.read_text()))[0])

    def test_atomic_replace_failure_preserves_existing_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            output = directory / "metar-live.json"
            self.u.write_json([live_record()], output)
            original = output.read_bytes()
            changed = [live_record(obs_time="2026-08-31T08:50:00Z")]
            with mock.patch.object(self.u.os, "replace", side_effect=OSError("blocked")):
                with self.assertRaises(OSError):
                    self.u.write_json(changed, output)
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(list(directory.glob(".metar-live-*.json")), [])

    def test_invalid_observation_time_and_invalid_records_are_ignored(self):
        payload = source_document(
            source_metar(observation_time="not-a-time"),
            source_metar("EHAM", raw="METAR EBAW 310820Z 35004KT CAVOK 19/13 Q1016"),
            source_metar("EGLL"),
        )
        allowed = {
            "EBAW": airport(),
            "EHAM": airport("EHAM", "Netherlands"),
            "EGLL": airport("EGLL", "United Kingdom"),
        }
        records, _, _, _, errors = self.u.build_records(
            self.u.parse_source(payload), allowed
        )
        self.assertEqual([record["icaoId"] for record in records], ["EGLL"])
        self.assertEqual(len(errors), 2)

    def test_deterministic_sort_and_latest_duplicate_selection(self):
        payload = source_document(
            source_metar("EHAM", observation_time="2026-08-31T08:10:00Z"),
            source_metar("EBAW"),
            source_metar("EHAM", observation_time="2026-08-31T08:30:00Z", raw="METAR EHAM 310830Z 01002KT CAVOK 18/12 Q1017"),
        )
        allowed = {
            "EBAW": airport(),
            "EHAM": airport("EHAM", "Netherlands"),
        }
        root = self.u.parse_source(payload)
        first = self.u.build_records(root, allowed)[0]
        second = self.u.build_records(root, allowed)[0]
        self.assertEqual(first, second)
        self.assertEqual([record["icaoId"] for record in first], ["EBAW", "EHAM"])
        self.assertEqual(first[1]["obsTime"], "2026-08-31T08:30:00Z")

    def test_validation_rejects_nan_duplicate_and_invalid_obs_time(self):
        record = live_record()
        broken_nan = [{**record, "temp": float("nan")}]
        self.assertFalse(self.u.validate_dataset(broken_nan)[0])
        self.assertFalse(self.u.validate_dataset([record, record])[0])
        self.assertFalse(self.u.validate_dataset([{**record, "obsTime": "bad"}])[0])
        self.assertFalse(self.u.validate_dataset([{**record, "fltCat": []}])[0])


if __name__ == "__main__":
    unittest.main()
