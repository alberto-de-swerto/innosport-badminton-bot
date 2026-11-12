from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import json

from sportsite import DataError, write_json
from violation_registry import ViolationRegistry
from test_sportsite import TemporaryProject, START, PEOPLE


def report(training_id=101, start=START, cancelled=None, checked=None):
    return {"schema_version": 1, "training_id": training_id, "group_id": 17,
            "training_start": start.isoformat(), "training_end": (start + timedelta(hours=2)).isoformat(),
            "checked_at": (checked or start + timedelta(hours=3)).isoformat(),
            "baseline_mode": "scheduled", "preliminary": False,
            "registered": deepcopy(PEOPLE),
            "cancelled": deepcopy([PEOPLE[1]] if cancelled is None else cancelled)}


class RegistryTests(TemporaryProject):
    def setUp(self):
        super().setUp()
        self.registry = ViolationRegistry(self.config)

    def test_repeated_checks_and_restarts_count_each_session_once(self):
        self.registry.record(report())
        self.registry.record(report(checked=START + timedelta(hours=4)))
        self.registry.record(report())
        rows = ViolationRegistry(self.config).rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["student_id"], PEOPLE[1]["student_id"])
        self.assertEqual(rows[0]["count"], 1)
        self.assertEqual(self.registry.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.registry.path.parent.stat().st_mode & 0o777, 0o700)

    def test_multiple_dates_same_person_and_identical_names_use_ids(self):
        self.registry.record(report())
        self.registry.record(report(102, START + timedelta(days=2)))
        changed = report(103, START + timedelta(days=3), [PEOPLE[0]])
        changed["cancelled"][0]["full_name"] = PEOPLE[1]["full_name"]
        self.registry.record(changed)
        rows = self.registry.rows()
        self.assertEqual([p["count"] for p in rows], [2, 1])
        self.assertEqual({p["student_id"] for p in rows}, {1, 2})
        self.assertEqual(rows[0]["incidents"][0]["training_start"], (START + timedelta(days=2)).isoformat())

    def test_latest_correction_removes_incident_and_old_reports_cannot_restore_it(self):
        self.registry.record(report())
        self.registry.record(report(cancelled=[], checked=START + timedelta(hours=4)))
        write_json(self.root / "reports" / "older" / "report.json", report())
        self.registry.sync_history()
        self.assertEqual(self.registry.rows(), [])

    def test_dismiss_survives_recheck_history_and_restart_but_new_date_counts(self):
        self.registry.record(report())
        row = self.registry.rows()[0]
        self.registry.dismiss(row["student_id"], row["revision"])
        write_json(self.root / "reports" / "old" / "report.json", report())
        restarted = ViolationRegistry(self.config)
        restarted.sync_history()
        restarted.record(report(checked=START + timedelta(hours=4)))
        self.assertEqual(restarted.rows(), [])
        restarted.record(report(102, START + timedelta(days=2)))
        self.assertEqual(restarted.rows()[0]["count"], 1)

    def test_stale_delete_cannot_erase_new_incidents_and_double_click_is_safe(self):
        self.registry.record(report())
        old = self.registry.rows()[0]
        self.registry.record(report(102, START + timedelta(days=2)))
        self.assertIn("изменился", self.registry.dismiss(old["student_id"], old["revision"]))
        current = self.registry.rows()[0]
        self.assertEqual(current["count"], 2)
        self.assertIn("Удалено", self.registry.dismiss(current["student_id"], current["revision"]))
        self.assertIn("уже удалена", self.registry.dismiss(current["student_id"], current["revision"]))

    def test_backfill_uses_latest_report_and_excludes_tests_and_preliminary_results(self):
        old = report()
        latest = report(cancelled=[], checked=START + timedelta(hours=4))
        for name, data in (("z-old", old), ("a-latest", latest),
                           ("test", dict(report(102), baseline_mode="user_import")),
                           ("preliminary", dict(report(103), preliminary=True))):
            write_json(self.root / "reports" / name / "report.json", data)
        self.registry.sync_history()
        self.assertEqual(self.registry.rows(), [])
        self.assertEqual(len(self.registry.load()["sessions"]), 1)

    def test_invalid_report_does_not_modify_register_and_corruption_fails_closed(self):
        self.registry.record(report())
        original = self.registry.path.read_bytes()
        for changes in ({"cancelled": [PEOPLE[1], PEOPLE[1]]}, {"training_id": True},
                        {"registered": []}, {"baseline_mode": "manual"},
                        {"checked_at": START.isoformat()}):
            with self.subTest(changes=changes), self.assertRaises(DataError):
                self.registry.record(dict(report(), **changes))
            self.assertEqual(self.registry.path.read_bytes(), original)
        self.registry.path.write_text('{"schema_version": 99}')
        with self.assertRaises(DataError):
            self.registry.record(report())
        self.assertEqual(json.loads(self.registry.path.read_text()), {"schema_version": 99})

    def test_overlapping_workers_do_not_lose_sessions(self):
        reports = [report(ident, START + timedelta(days=ident - 101)) for ident in range(101, 121)]
        with ThreadPoolExecutor(max_workers=5) as workers:
            list(workers.map(lambda data: ViolationRegistry(self.config).record(data), reports))
        self.assertEqual(self.registry.rows()[0]["count"], 20)
