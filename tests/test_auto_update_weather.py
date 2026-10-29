#!/usr/bin/env python3

import importlib.util
import json
import os
import socket
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "auto-update-weather.py"


def load_updater():
    spec = importlib.util.spec_from_file_location("auto_update_weather", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AutoUpdateWeatherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.u = load_updater()
        cls.now = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)

    def recent_document(self, icao, generated):
        return {
            "schemaVersion": 1,
            "icao": icao,
            "generatedAt": self.u.iso_timestamp(generated),
            "recordCount": 0,
            "observations": [],
        }

    def archive_index(self, updated, stations=("EBAW",)):
        station_records = {
            icao: {
                "years": {"2026": ["08"]},
                "earliestObsTime": "2026-08-01T00:00:00Z",
                "latestObsTime": "2026-08-30T11:00:00Z",
                "recordCount": 10,
            }
            for icao in stations
        }
        return {
            "schemaVersion": 1,
            "generatedAt": self.u.iso_timestamp(updated),
            "updatedAt": self.u.iso_timestamp(updated),
            "stationCount": len(station_records),
            "recordCount": 10 * len(station_records),
            "stations": station_records,
        }

    def trends_document(self, icao, generated):
        return {
            "schemaVersion": 1,
            "icao": icao,
            "generatedAt": self.u.iso_timestamp(generated),
            "timezone": "Europe/Brussels",
            "coverage": {
                "firstObservation": "2026-08-01T00:00:00Z",
                "lastObservation": "2026-08-30T11:00:00Z",
            },
            "definitions": {},
            "averages": {},
            "years": {
                "2026": {
                    "8": {
                        "partialMonth": True,
                        "quality": {},
                        "vfrPercent": 90.0,
                        "mvfrOrBetterPercent": 99.0,
                        "maxTemperatureC": 26.0,
                        "minTemperatureC": 14.0,
                        "relativeHumidityPercent": 62.0,
                        "windSpeedKt": 6.0,
                    }
                }
            },
        }

    def test_cadence_constants_and_taf_boundary_are_unchanged(self):
        self.assertEqual(self.u.UPDATE_INTERVAL_SECONDS, 10 * 60)
        self.assertEqual(self.u.TAF_UPDATE_INTERVAL_SECONDS, 30 * 60)
        self.assertEqual(self.u.RECENT_UPDATE_INTERVAL_SECONDS, 60 * 60)
        self.assertEqual(self.u.ARCHIVE_UPDATE_INTERVAL_SECONDS, 10 * 60)
        self.assertEqual(self.u.TRENDS_UPDATE_INTERVAL_SECONDS, 60 * 60)
        self.assertEqual(self.u.ARCHIVE_RECONCILE_INTERVAL_SECONDS, 24 * 60 * 60)

        taf = {
            "schemaVersion": 1,
            "generatedAt": self.u.iso_timestamp(self.now),
            "availableStationCount": 1,
            "airports": {
                "EBAW": {
                    "icaoId": "EBAW",
                    "rawTAF": "TAF EBAW",
                    "issueTime": "2026-08-30T11:00:00Z",
                    "validFrom": "2026-08-30T11:00:00Z",
                    "validTo": "2026-08-31T11:00:00Z",
                    "forecastGroups": [
                        {
                            "sequence": 0,
                            "type": "BASELINE",
                            "validFrom": "2026-08-30T11:00:00Z",
                            "validTo": "2026-08-31T11:00:00Z",
                        }
                    ],
                }
            },
        }
        self.assertFalse(self.u.taf_update_due(taf, self.now + timedelta(minutes=29, seconds=59))[0])
        self.assertTrue(self.u.taf_update_due(taf, self.now + timedelta(minutes=30))[0])

    def test_recent_history_due_and_not_due(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for icao in ("EBAW", "EHAM"):
                (root / f"{icao}.json").write_text(
                    json.dumps(self.recent_document(icao, self.now))
                )
            expected = {"EBAW", "EHAM"}
            self.assertFalse(
                self.u.recent_update_due(
                    root, expected, self.now + timedelta(minutes=59, seconds=59)
                )[0]
            )
            self.assertTrue(
                self.u.recent_update_due(root, expected, self.now + timedelta(hours=1))[0]
            )
            (root / "EHAM.json").unlink()
            self.assertTrue(self.u.recent_update_due(root, expected, self.now)[0])

    def test_archive_due_and_not_due(self):
        index = self.archive_index(self.now)
        self.assertFalse(
            self.u.archive_update_due(
                index, {"EBAW"}, self.now + timedelta(minutes=9, seconds=59)
            )[0]
        )
        self.assertTrue(
            self.u.archive_update_due(index, {"EBAW"}, self.now + timedelta(minutes=10))[0]
        )
        self.assertTrue(self.u.archive_update_due(index, {"EBAW", "EHAM"}, self.now)[0])

    def test_trends_due_and_not_due(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "EBAW.json").write_text(
                json.dumps(self.trends_document("EBAW", self.now))
            )
            self.assertFalse(
                self.u.trends_update_due(
                    root, {"EBAW"}, self.now + timedelta(minutes=59, seconds=59)
                )[0]
            )
            self.assertTrue(
                self.u.trends_update_due(
                    root, {"EBAW"}, self.now + timedelta(hours=1)
                )[0]
            )
            (root / "EBAW.json").unlink()
            self.assertTrue(self.u.trends_update_due(root, {"EBAW"}, self.now)[0])

    def test_daily_archive_reconciliation_due(self):
        state = {
            "schemaVersion": 1,
            "archiveLastReconciledAt": self.u.iso_timestamp(self.now),
        }
        self.assertFalse(
            self.u.archive_reconciliation_due(
                state, self.now + timedelta(hours=23, minutes=59)
            )[0]
        )
        self.assertTrue(
            self.u.archive_reconciliation_due(state, self.now + timedelta(days=1))[0]
        )
        self.assertTrue(self.u.archive_reconciliation_due({}, self.now)[0])

    def test_archive_lock_conflict_is_safe_skip(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Archive update failed: Another archive writer holds lock",
        )
        validator = mock.Mock(return_value=(True, "valid"))
        with mock.patch.object(self.u.subprocess, "run", return_value=completed):
            result = self.u.run_external_task(
                "Archive",
                [str(self.u.ARCHIVE_SCRIPT)],
                validator,
                lock_conflict_is_skip=True,
            )
        self.assertEqual(result, "skipped")
        validator.assert_not_called()

    def cycle_fixture(self, directory):
        directory = Path(directory)
        live = directory / "metar-live.json"
        taf = directory / "taf-live.json"
        archive = directory / "index.json"
        state = directory / "state.json"
        recent = directory / "recent"
        trends = directory / "trends"
        recent.mkdir()
        trends.mkdir()
        live.write_text(json.dumps([{"icaoId": "EBAW", "rawOb": "METAR EBAW"}]))
        taf.write_text(json.dumps({}))
        archive.write_text(json.dumps(self.archive_index(self.now - timedelta(minutes=10))))
        state.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "archiveLastReconciledAt": self.u.iso_timestamp(self.now),
                }
            )
        )
        (recent / "EBAW.json").write_text(json.dumps(self.recent_document("EBAW", self.now)))
        (trends / "EBAW.json").write_text(json.dumps(self.trends_document("EBAW", self.now)))
        return live, taf, archive, state, recent, trends

    def test_valid_archive_automatic_run_never_uses_30_day_backfill(self):
        with tempfile.TemporaryDirectory() as directory:
            live, taf, archive, state, recent, trends = self.cycle_fixture(directory)
            with mock.patch.multiple(
                self.u,
                LIVE_PATH=live,
                TAF_PATH=taf,
                ARCHIVE_INDEX_PATH=archive,
                SCHEDULER_STATE_PATH=state,
                RECENT_DIR=recent,
                TRENDS_DIR=trends,
            ), mock.patch.object(self.u, "run_update", return_value=True), mock.patch.object(
                self.u, "taf_update_due", return_value=(False, "not due")
            ), mock.patch.object(
                self.u, "recent_update_due", return_value=(False, "not due")
            ), mock.patch.object(
                self.u, "archive_update_due", return_value=(True, "due")
            ), mock.patch.object(
                self.u, "archive_reconciliation_due", return_value=(False, "not due")
            ), mock.patch.object(
                self.u, "run_external_task", return_value="success"
            ) as task:
                self.assertTrue(self.u.run_cycle(self.now))
            archive_call = task.call_args
            command = archive_call.args[1]
            self.assertEqual(command, [str(self.u.ARCHIVE_SCRIPT)])
            self.assertNotIn("--backfill-days", command)

    def test_missing_trends_output_runs_at_startup_when_archive_is_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            live, taf, archive, state, recent, trends = self.cycle_fixture(directory)
            (trends / "EBAW.json").unlink()
            with mock.patch.multiple(
                self.u,
                LIVE_PATH=live,
                TAF_PATH=taf,
                ARCHIVE_INDEX_PATH=archive,
                SCHEDULER_STATE_PATH=state,
                RECENT_DIR=recent,
                TRENDS_DIR=trends,
            ), mock.patch.object(self.u, "run_update", return_value=True), mock.patch.object(
                self.u, "taf_update_due", return_value=(False, "not due")
            ), mock.patch.object(
                self.u, "recent_update_due", return_value=(False, "not due")
            ), mock.patch.object(
                self.u, "archive_update_due", return_value=(False, "not due")
            ), mock.patch.object(
                self.u, "archive_reconciliation_due", return_value=(False, "not due")
            ), mock.patch.object(
                self.u, "run_external_task", return_value="success"
            ) as task:
                self.assertTrue(self.u.run_cycle(self.now))
            task.assert_called_once()
            self.assertEqual(task.call_args.args[0], "Trends")
            self.assertEqual(task.call_args.args[1], [str(self.u.TRENDS_SCRIPT)])

    def test_recent_valid_trends_output_does_not_run_at_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            live, taf, archive, state, recent, trends = self.cycle_fixture(directory)
            with mock.patch.multiple(
                self.u,
                LIVE_PATH=live,
                TAF_PATH=taf,
                ARCHIVE_INDEX_PATH=archive,
                SCHEDULER_STATE_PATH=state,
                RECENT_DIR=recent,
                TRENDS_DIR=trends,
            ), mock.patch.object(self.u, "run_update", return_value=True), mock.patch.object(
                self.u, "taf_update_due", return_value=(False, "not due")
            ), mock.patch.object(
                self.u, "recent_update_due", return_value=(False, "not due")
            ), mock.patch.object(
                self.u, "archive_update_due", return_value=(False, "not due")
            ), mock.patch.object(
                self.u, "archive_reconciliation_due", return_value=(False, "not due")
            ), mock.patch.object(self.u, "run_external_task") as task:
                self.assertTrue(self.u.run_cycle(self.now))
            task.assert_not_called()

    def test_trends_runs_after_archive_becomes_available_without_backfill_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            live, taf, archive, state, recent, trends = self.cycle_fixture(directory)
            archive.write_text("{}")
            (trends / "EBAW.json").unlink()

            def external(label, command, *args, **kwargs):
                if label == "Archive":
                    archive.write_text(json.dumps(self.archive_index(self.now)))
                return "success"

            with mock.patch.multiple(
                self.u,
                LIVE_PATH=live,
                TAF_PATH=taf,
                ARCHIVE_INDEX_PATH=archive,
                SCHEDULER_STATE_PATH=state,
                RECENT_DIR=recent,
                TRENDS_DIR=trends,
            ), mock.patch.object(self.u, "run_update", return_value=True), mock.patch.object(
                self.u, "taf_update_due", return_value=(False, "not due")
            ), mock.patch.object(
                self.u, "recent_update_due", return_value=(False, "not due")
            ), mock.patch.object(
                self.u, "archive_reconciliation_due", return_value=(False, "not due")
            ), mock.patch.object(
                self.u, "run_external_task", side_effect=external
            ) as task:
                self.assertTrue(self.u.run_cycle(self.now))
            self.assertEqual([call.args[0] for call in task.call_args_list], ["Archive", "Trends"])
            self.assertEqual(task.call_args_list[0].args[1], [str(self.u.ARCHIVE_SCRIPT)])
            self.assertNotIn("--backfill-days", task.call_args_list[0].args[1])

    def test_invalid_archive_skips_trends_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            live, taf, archive, state, recent, trends = self.cycle_fixture(directory)
            archive.write_text("{}")
            with mock.patch.multiple(
                self.u,
                LIVE_PATH=live,
                TAF_PATH=taf,
                ARCHIVE_INDEX_PATH=archive,
                SCHEDULER_STATE_PATH=state,
                RECENT_DIR=recent,
                TRENDS_DIR=trends,
            ), mock.patch.object(self.u, "run_update", return_value=True), mock.patch.object(
                self.u, "taf_update_due", return_value=(False, "not due")
            ), mock.patch.object(
                self.u, "recent_update_due", return_value=(False, "not due")
            ), mock.patch.object(
                self.u, "archive_reconciliation_due", return_value=(False, "not due")
            ), mock.patch.object(
                self.u, "run_external_task", return_value="failure"
            ) as task:
                self.assertFalse(self.u.run_cycle(self.now))
            self.assertEqual([call.args[0] for call in task.call_args_list], ["Archive"])

    def test_trends_failure_does_not_block_other_updater_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            live, taf, archive, state, recent, trends = self.cycle_fixture(directory)
            (trends / "EBAW.json").unlink()

            def external(label, *args, **kwargs):
                return "failure" if label == "Trends" else "success"

            with mock.patch.multiple(
                self.u,
                LIVE_PATH=live,
                TAF_PATH=taf,
                ARCHIVE_INDEX_PATH=archive,
                SCHEDULER_STATE_PATH=state,
                RECENT_DIR=recent,
                TRENDS_DIR=trends,
            ), mock.patch.object(self.u, "run_update", return_value=True) as core, mock.patch.object(
                self.u, "taf_update_due", return_value=(True, "due")
            ), mock.patch.object(
                self.u, "recent_update_due", return_value=(True, "due")
            ), mock.patch.object(
                self.u, "archive_update_due", return_value=(True, "due")
            ), mock.patch.object(
                self.u, "archive_reconciliation_due", return_value=(False, "not due")
            ), mock.patch.object(
                self.u, "run_external_task", side_effect=external
            ) as task:
                self.assertFalse(self.u.run_cycle(self.now))
            self.assertEqual([call.args[0] for call in task.call_args_list], ["Recent History", "Archive", "Trends"])
            self.assertEqual([call.args[0] for call in core.call_args_list], ["Live", "History", "TAF"])

    def test_daily_reconciliation_uses_seven_days_and_records_success(self):
        with tempfile.TemporaryDirectory() as directory:
            live, taf, archive, state, recent, trends = self.cycle_fixture(directory)
            with mock.patch.multiple(
                self.u,
                LIVE_PATH=live,
                TAF_PATH=taf,
                ARCHIVE_INDEX_PATH=archive,
                SCHEDULER_STATE_PATH=state,
                RECENT_DIR=recent,
                TRENDS_DIR=trends,
            ), mock.patch.object(self.u, "run_update", return_value=True), mock.patch.object(
                self.u, "taf_update_due", return_value=(False, "not due")
            ), mock.patch.object(
                self.u, "recent_update_due", return_value=(False, "not due")
            ), mock.patch.object(
                self.u, "archive_update_due", return_value=(False, "not due")
            ), mock.patch.object(
                self.u, "archive_reconciliation_due", return_value=(True, "due")
            ), mock.patch.object(
                self.u, "run_external_task", return_value="success"
            ) as task, mock.patch.object(self.u, "write_scheduler_state_atomic") as write_state:
                self.assertTrue(self.u.run_cycle(self.now))
            command = task.call_args.args[1]
            self.assertEqual(
                command,
                [str(self.u.ARCHIVE_SCRIPT), "--reconcile-days", "7"],
            )
            write_state.assert_called_once()

    def test_task_failure_does_not_block_later_independent_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            live, taf, archive, state, recent, trends = self.cycle_fixture(directory)

            def core_result(label, *args, **kwargs):
                return label not in {"Live", "TAF"}

            with mock.patch.multiple(
                self.u,
                LIVE_PATH=live,
                TAF_PATH=taf,
                ARCHIVE_INDEX_PATH=archive,
                SCHEDULER_STATE_PATH=state,
                RECENT_DIR=recent,
                TRENDS_DIR=trends,
            ), mock.patch.object(self.u, "run_update", side_effect=core_result), mock.patch.object(
                self.u, "taf_update_due", return_value=(True, "due")
            ), mock.patch.object(
                self.u, "recent_update_due", return_value=(True, "due")
            ), mock.patch.object(
                self.u, "archive_update_due", return_value=(True, "due")
            ), mock.patch.object(
                self.u, "archive_reconciliation_due", return_value=(False, "not due")
            ), mock.patch.object(
                self.u, "run_external_task", side_effect=["failure", "success"]
            ) as external:
                self.assertFalse(self.u.run_cycle(self.now))
            self.assertEqual(external.call_count, 2)
            self.assertEqual(external.call_args_list[0].args[0], "Recent History")
            self.assertEqual(external.call_args_list[1].args[0], "Archive")

    def test_main_process_lock_blocks_second_instance_and_recovers_stale_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / ".weather-updater.lock"
            first = self.u.ArchiveLock(path=lock_path)
            first.acquire()
            try:
                with mock.patch.object(self.u, "WEATHER_LOCK_PATH", lock_path):
                    self.assertEqual(self.u.main(["--once"]), 1)
            finally:
                first.release()

            stale = {
                "schemaVersion": 1,
                "pid": 99999999,
                "hostname": socket.gethostname(),
                "startedAt": "2026-01-01T00:00:00Z",
                "token": "dead",
            }
            lock_path.write_text(json.dumps(stale))
            with mock.patch.object(self.u, "WEATHER_LOCK_PATH", lock_path), mock.patch.object(
                self.u, "run_cycle", return_value=True
            ):
                self.assertEqual(self.u.main(["--once"]), 0)
            self.assertFalse(lock_path.exists())

    def test_shutdown_request_releases_main_process_lock_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / ".weather-updater.lock"
            with mock.patch.object(self.u, "WEATHER_LOCK_PATH", lock_path), mock.patch.object(
                self.u, "run_cycle", side_effect=self.u.ShutdownRequested("SIGTERM")
            ):
                self.assertEqual(self.u.main([]), 0)
            self.assertFalse(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
