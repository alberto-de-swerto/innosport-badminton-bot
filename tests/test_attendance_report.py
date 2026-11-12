from contextlib import contextmanager
from copy import deepcopy
from datetime import date, timedelta
import json
import unittest
from unittest import mock

import attendance_report as reporting
import sportsite
from violation_registry import ViolationRegistry
from test_sportsite import TemporaryProject, START, DUE, EVENT, PEOPLE, GRADES, DETAIL, fake_connection


class ReportTests(TemporaryProject):
    def baseline(self, scheduled=True):
        return sportsite.collect(self.config, START, scheduled=scheduled,
                                 clock=lambda: DUE, connector=fake_connection)

    def connector(self, rows=None, event=None):
        api = mock.Mock()
        api.find.return_value = event or deepcopy(EVENT)
        detail = deepcopy(DETAIL)
        detail["training"]["end"] = (START + timedelta(hours=2)).isoformat()
        api.get.return_value = detail
        api.attendance.return_value = rows if rows is not None else [dict(PEOPLE[0], hours=2)]
        @contextmanager
        def connector(config):
            yield api
        return connector, api

    def test_uses_only_scheduled_baseline_and_never_changes_it(self):
        path = self.baseline()
        original = (path / "snapshot.json").read_bytes()
        self.baseline(False)
        connector, api = self.connector()
        result = reporting.build_report(self.config, START.date(), connector=connector,
                                        clock=lambda: START + timedelta(hours=3))
        self.assertEqual(len(result["registered"]), 2)
        self.assertEqual(result["cancelled"], [PEOPLE[1]])
        self.assertEqual(result["attended"][0]["hours"], 2)
        self.assertFalse(result["preliminary"])
        api.attendance.assert_called_once()
        output = reporting.save_report(self.config, result)
        self.assertEqual(ViolationRegistry(self.config).rows()[0]["student_id"], PEOPLE[1]["student_id"])
        self.assertEqual((output / "report.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((output / "report.txt").stat().st_mode & 0o777, 0o600)
        self.assertEqual((path / "snapshot.json").read_bytes(), original)

    def test_missing_or_only_manual_snapshot_never_queries_the_site(self):
        for manual in (False, True):
            if manual:
                self.baseline(False)
            connector = mock.Mock()
            with self.assertRaisesRegex(sportsite.SportError, "Нет планового снимка"):
                reporting.build_report(self.config, START.date(), connector=connector,
                                       clock=lambda: START + timedelta(hours=3))
            connector.assert_not_called()

    def test_does_not_silently_compare_an_older_day_if_latest_snapshot_is_missing(self):
        self.baseline()
        with self.assertRaisesRegex(sportsite.SportError, "07.11.2026"):
            reporting.build_report(self.config, connector=mock.Mock(),
                                   clock=lambda: START + timedelta(days=2, hours=2))

    def test_wrong_date_future_date_and_ambiguous_group_fail(self):
        with self.assertRaises(sportsite.SportError):
            reporting.training_start(self.config, date(2026, 11, 6), START)
        with self.assertRaisesRegex(sportsite.SportError, "ещё не началась"):
            reporting.training_start(self.config, START.date(), DUE)
        self.baseline()
        changed = deepcopy(EVENT)
        changed["extendedProps"]["id"] = 999
        connector, api = self.connector(event=changed)
        with self.assertRaisesRegex(sportsite.DataError, "другое занятие"):
            reporting.build_report(self.config, START.date(), connector=connector, clock=lambda: START)
        api.attendance.assert_not_called()

    def test_corrupted_count_duplicate_id_late_or_relabelled_manual_fails(self):
        path = self.baseline() / "snapshot.json"
        original = json.loads(path.read_text())
        cases = [dict(original, participant_count=0), dict(original, mode="manual"),
                 dict(original, participants=[PEOPLE[0], PEOPLE[0]]),
                 dict(original, captured_at=(DUE + timedelta(minutes=5)).isoformat()),
                 dict(original, group_id=999, title="Other club")]
        for damaged in cases:
            with self.subTest(damaged=damaged):
                path.write_text(json.dumps(damaged))
                with self.assertRaises(sportsite.DataError):
                    reporting.load_baseline(self.config, START)

    def test_preliminary_and_zero_hours_warning(self):
        self.baseline()
        connector, _ = self.connector(rows=[dict(person, hours=0) for person in PEOPLE])
        result = reporting.build_report(self.config, START.date(), connector=connector, clock=lambda: START)
        text = reporting.text_report(result)
        self.assertIn("предварительное", text)
        self.assertIn("нет ни одной положительной отметки", text)
        self.assertIn("Не пришли", text)

    def test_capture_time_excludes_vpn_restore_delay(self):
        self.baseline()
        calls = []
        connector, api = self.connector()
        @contextmanager
        def restoring(config):
            with connector(config) as connected:
                yield connected
            calls.append("restored")
        def clock():
            self.assertNotIn("restored", calls)
            return START + timedelta(hours=3)
        result = reporting.build_report(self.config, START.date(), connector=restoring, clock=clock)
        self.assertTrue(calls)
        self.assertEqual(result["checked_at"], (START + timedelta(hours=3)).isoformat())


class ImportedListTests(TemporaryProject):
    baseline = ReportTests.baseline
    connector = ReportTests.connector
    def import_list(self, **changes):
        value = dict(schema_version=1, mode="user_import", training_id=101, group_id=17,
                     title=EVENT["title"], training_start=START.isoformat(),
                     reference_at=DUE.isoformat(), reference_at_basis="user_statement",
                     imported_at=(START + timedelta(days=2)).isoformat(), source_kind="user_screenshots",
                     source_files=["source-1.png", "source-2.png"],
                     participant_count=len(PEOPLE), participants=deepcopy(PEOPLE))
        value.update(changes)
        path = self.root / "imports" / self.config.job_key(START) / "baseline.json"
        sportsite.write_json(path, value)
        return path

    def test_explicit_import_is_used_and_labelled_without_backdating_capture(self):
        path = self.import_list()
        original = path.read_bytes()
        connector, _ = self.connector()
        result = reporting.build_report(self.config, START.date(), connector=connector,
                                        clock=lambda: START + timedelta(days=2))
        self.assertEqual(result["baseline_mode"], "user_import")
        self.assertIsNone(result["snapshot_at"])
        self.assertEqual(result["reference_at"], DUE.isoformat())
        text = reporting.text_report(result)
        self.assertIn("Тест по списку со скриншотов", text)
        self.assertIn("время со слов пользователя", text)
        self.assertEqual([p["student_id"] for p in result["cancelled"]], [2])
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.root / ".state/jobs.json").exists())
        self.assertFalse((self.root / "exports").exists())

    def test_real_scheduled_snapshot_has_priority_over_user_import(self):
        self.import_list(participants=[], participant_count=0)
        self.baseline()
        self.assertEqual(reporting.load_baseline(self.config, START)["mode"], "scheduled")

    def test_manual_exports_are_not_implicitly_imported(self):
        self.baseline(False)
        with self.assertRaisesRegex(sportsite.SportError, "Нет планового снимка"):
            reporting.load_baseline(self.config, START)

    def test_invalid_import_cannot_claim_to_be_a_scheduled_capture(self):
        for changes in ({"mode": "scheduled"}, {"captured_at": DUE.isoformat()},
                        {"reference_at_basis": "automatic"}, {"source_kind": "unknown"},
                        {"participant_count": 0}, {"participants": [PEOPLE[0], PEOPLE[0]]},
                        {"training_start": DUE.isoformat()}, {"source_files": []}):
            with self.subTest(changes=changes):
                self.import_list(**changes)
                with self.assertRaises(sportsite.DataError):
                    reporting.load_baseline(self.config, START)

    def test_import_does_not_mark_missed_job_as_saved(self):
        self.import_list()
        sportsite.Scheduler(self.config).tick(START + timedelta(hours=3))
        jobs = json.loads((self.root / ".state/jobs.json").read_text())
        self.assertEqual(jobs[self.config.job_key(START)]["status"], "missed")

    def test_corrupted_scheduled_capture_does_not_fall_back_to_import(self):
        self.import_list()
        path = self.baseline() / "snapshot.json"
        path.write_text("{}")
        with self.assertRaises(sportsite.DataError):
            reporting.load_baseline(self.config, START)


class ComparisonTests(unittest.TestCase):
    def test_ids_drive_all_categories_including_same_name_and_late_additions(self):
        before = {"participants": [{"student_id": i, "full_name": "Тёзка"} for i in range(1, 5)]}
        rows = [{"student_id": 1, "full_name": "Новое имя", "hours": 2},
                {"student_id": 2, "full_name": "Тёзка", "hours": 0},
                {"student_id": 5, "full_name": "Поздний игрок", "hours": 1},
                {"student_id": 6, "full_name": "Поздний без часов", "hours": 0}]
        result = reporting.compare(before, rows)
        self.assertEqual({p["student_id"] for p in result["cancelled"]}, {3, 4})
        self.assertEqual([p["student_id"] for p in result["no_hours"]], [2])
        self.assertEqual([p["student_id"] for p in result["new_no_hours"]], [6])
        self.assertEqual({p["student_id"]: p["in_snapshot"] for p in result["attended"]}, {1: True, 5: False})
        self.assertEqual(len(result["registered"]), 4)


class AttendanceAPITests(TemporaryProject):
    def api_with(self, raw):
        api = sportsite.SportAPI(mock.Mock(), self.config)
        api.get = mock.Mock(return_value=raw)
        return api

    def test_retains_only_id_name_hours_and_does_not_compare_union_to_checkin_count(self):
        raw = deepcopy(GRADES)
        raw["grades"][0]["hours"] = 2
        api = self.api_with(raw)
        people = api.attendance(EVENT, START)
        self.assertEqual(len(people), 2)
        self.assertTrue(all(set(p) == {"student_id", "full_name", "hours"} for p in people))
        api.get.assert_called_once_with("/api/attendance/101/grades")

    def test_invalid_or_missing_marks_never_become_absences(self):
        for hours in (None, True, "0", -1, float("nan"), float("inf")):
            raw = deepcopy(GRADES)
            raw["grades"][0]["hours"] = hours
            with self.subTest(hours=hours), self.assertRaises(sportsite.DataError):
                self.api_with(raw).attendance(EVENT, START)
        for field, value in (("group_id", 999), ("start", DUE.isoformat()), ("grades", None)):
            raw = deepcopy(GRADES)
            raw[field] = value
            with self.assertRaises(sportsite.DataError):
                self.api_with(raw).attendance(EVENT, START)
        raw = deepcopy(GRADES)
        raw["grades"].append(raw["grades"][0])
        with self.assertRaises(sportsite.DataError):
            self.api_with(raw).attendance(EVENT, START)


if __name__ == "__main__":
    unittest.main()
