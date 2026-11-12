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

    def test_decrement_forgives_oldest_case_then_removes_person_at_zero(self):
        self.registry.record(report())
        later = report(102, START + timedelta(days=2))
        self.registry.record(later)
        old = self.registry.rows()[0]
        self.assertIn("2 → 1", self.registry.decrement(old["student_id"], old["revision"]))
        row = self.registry.rows()[0]
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["incidents"][0]["training_start"], later["training_start"])
        self.assertIn("изменился", self.registry.decrement(old["student_id"], old["revision"]))
        self.assertIn("Счётчик: 0", self.registry.decrement(row["student_id"], row["revision"]))
        self.assertEqual(self.registry.rows(), [])
        self.registry.record(report(checked=START + timedelta(days=4)))
        self.registry.record(dict(later, checked_at=(START + timedelta(days=4)).isoformat()))
        self.assertEqual(ViolationRegistry(self.config).rows(), [])

    def test_explicit_import_confirmation_counts_only_selected_person_and_keeps_source(self):
        imported = dict(report(cancelled=PEOPLE), baseline_mode="user_import")
        write_json(self.root / "reports" / "imported" / "report.json", imported)
        self.registry.sync_history()
        self.assertEqual(self.registry.rows(), [])
        self.registry.confirm_import(imported, PEOPLE[1]["student_id"])
        self.registry.confirm_import(imported, PEOPLE[1]["student_id"])
        self.registry.record(report(102, START + timedelta(days=2)))
        restarted = ViolationRegistry(self.config)
        restarted.sync_history()
        rows = restarted.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 2)
        self.assertEqual(rows[0]["incidents"][-1]["source"], "confirmed_import")
        restarted.decrement(rows[0]["student_id"], rows[0]["revision"])
        restarted.confirm_import(imported, PEOPLE[1]["student_id"])
        restarted.sync_history()
        self.assertEqual(restarted.rows()[0]["count"], 1)
        self.assertEqual(restarted.load()["confirmed_cases"], {})

    def test_import_confirmation_rejects_unrelated_person_preliminary_and_scheduled_reports(self):
        for data, ident in ((report(), 2), (dict(report(), baseline_mode="user_import"), 1),
                            (dict(report(), baseline_mode="user_import", preliminary=True), 2)):
            with self.subTest(data=data, ident=ident), self.assertRaises(DataError):
                self.registry.confirm_import(data, ident)
        self.assertEqual(self.registry.rows(), [])

    def test_old_state_loads_with_empty_confirmations_without_losing_cases(self):
        self.registry.record(report())
        state = self.registry.load()
        del state["confirmed_cases"]
        write_json(self.registry.path, state)
        self.assertEqual(self.registry.rows()[0]["count"], 1)
        self.registry.record(report(102, START + timedelta(days=2)))
        self.assertEqual(self.registry.load()["confirmed_cases"], {})
        self.assertEqual(self.registry.rows()[0]["count"], 2)

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

    def test_undo_restores_decrement_after_restart_and_preserves_new_cases(self):
        self.registry.record(report())
        row = self.registry.rows()[0]
        self.registry.decrement(row["student_id"], row["revision"])
        action = self.registry.latest_undo()
        self.assertEqual(self.registry.rows(), [])
        self.registry.record(report(102, START + timedelta(days=2)))
        restarted = ViolationRegistry(self.config)
        self.assertIn("Восстановлено случаев: 1", restarted.undo(expected_id=action))
        self.assertEqual(restarted.rows()[0]["count"], 2)
        self.assertIsNone(restarted.latest_undo())

    def test_undo_restores_full_deletion_and_confirmed_import_with_source(self):
        self.registry.confirm_import(dict(report(), baseline_mode="user_import"), 2)
        self.registry.record(report(102, START + timedelta(days=2)))
        row = self.registry.rows()[0]
        self.registry.dismiss(2, row["revision"])
        self.assertEqual(self.registry.rows(), [])
        self.assertIn("Восстановлено случаев: 2", self.registry.undo())
        restored = self.registry.rows()[0]
        self.assertEqual(restored["count"], 2)
        self.assertEqual(restored["incidents"][-1]["source"], "confirmed_import")

    def test_undo_request_retry_is_idempotent_and_old_button_does_not_undo_another_action(self):
        self.registry.record(report())
        self.registry.record(report(102, START + timedelta(days=2)))
        row = self.registry.rows()[0]
        self.registry.decrement(2, row["revision"])
        old_action = self.registry.latest_undo()
        self.registry.decrement(2, self.registry.rows()[0]["revision"])
        self.assertIn("устарела", self.registry.undo(expected_id=old_action))
        result = self.registry.undo(request_id="update:100")
        self.assertEqual(self.registry.rows()[0]["count"], 1)
        self.assertEqual(self.registry.undo(request_id="update:100"), result)
        self.assertEqual(self.registry.rows()[0]["count"], 1)
        self.registry.undo(expected_id=old_action, request_id="update:101")
        self.assertEqual(self.registry.rows()[0]["count"], 2)

    def test_undo_does_not_resurrect_a_case_corrected_on_the_website(self):
        self.registry.record(report())
        row = self.registry.rows()[0]
        self.registry.decrement(2, row["revision"])
        self.registry.record(report(cancelled=[], checked=START + timedelta(hours=4)))
        answer = self.registry.undo()
        self.assertIn("не восстановлены: 1", answer)
        self.assertEqual(self.registry.rows(), [])

    def test_undo_history_is_bounded_and_empty_history_does_not_change_state(self):
        self.assertIn("Нет действий", self.registry.undo())
        for ident in range(101, 123):
            self.registry.record(report(ident, START + timedelta(days=ident - 101)))
            row = self.registry.rows()[0]
            self.registry.decrement(2, row["revision"])
        self.assertEqual(len(self.registry.load()["undo_history"]), 20)
