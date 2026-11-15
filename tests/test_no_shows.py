"""No-show policy regressions shared by the JSON and PostgreSQL implementations."""

from copy import deepcopy
from datetime import timedelta

from sportsite import DataError, write_json
from test_sportsite import PEOPLE, START, TemporaryProject
from test_violation_registry import report
from violation_registry import ViolationRegistry, report_session
import telegram_bot as bot


def no_show_report(ident=101, start=START, checked=None):
    return dict(report(ident, start, cancelled=[], checked=checked),
                no_hours=[dict(PEOPLE[1], hours=0)], attended=[dict(PEOPLE[0], hours=2)], new_no_hours=[])


class NoShowTests(TemporaryProject):
    def setUp(self):
        super().setUp()
        self.registry = ViolationRegistry(self.config)

    def test_no_show_is_counted_once_and_visible_with_type(self):
        self.registry.record(no_show_report())
        self.registry.record(no_show_report(checked=START + timedelta(hours=4)))
        row = self.registry.rows()[0]
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["incidents"][0]["violation_kind"], "no_show")
        text, _ = bot.violations_page(self.config, [row], language="en")
        self.assertIn("no-show", text)
        self.assertIn(PEOPLE[1]["full_name"], text)

    def test_no_show_and_cancellation_for_one_person_in_one_session_count_once(self):
        raw = no_show_report()
        raw["cancelled"] = [deepcopy(PEOPLE[1])]
        self.registry.record(raw)
        self.assertEqual(self.registry.rows()[0]["count"], 1)
        self.assertEqual(self.registry.rows()[0]["incidents"][0]["violation_kind"], "late_cancellation")

    def test_hours_correction_removes_no_show_and_undo_cannot_restore_it(self):
        self.registry.record(no_show_report())
        row = self.registry.rows()[0]
        self.registry.decrement(row["student_id"], row["revision"])
        self.registry.record(dict(report(cancelled=[], checked=START + timedelta(hours=4)),
                                  no_hours=[], attended=[dict(p, hours=2) for p in PEOPLE]))
        self.registry.undo(request_id="corrected-no-show")
        self.assertEqual(self.registry.rows(), [])

    def test_no_show_can_be_forgiven_undone_and_not_resurrected_by_backfill(self):
        raw = no_show_report()
        self.registry.record(raw)
        row = self.registry.rows()[0]
        self.registry.dismiss(row["student_id"], row["revision"])
        write_json(self.root/"reports/saved/report.json", raw)
        self.registry.sync_history()
        self.assertEqual(self.registry.rows(), [])
        self.registry.undo(request_id="undo-no-show")
        self.assertEqual(self.registry.rows()[0]["incidents"][0]["violation_kind"], "no_show")

    def test_equal_time_legacy_session_is_rebuilt_once_under_new_policy(self):
        raw = no_show_report()
        self.registry.record(report(cancelled=[]))
        state = self.registry.load()
        state["sessions"]["17:101"].pop("policy_version")
        write_json(self.registry.path, state)
        write_json(self.root/"reports/saved/report.json", raw)
        self.registry.sync_history()
        self.assertEqual(self.registry.rows()[0]["count"], 1)
        self.assertEqual(self.registry.load()["sessions"]["17:101"]["policy_version"], 2)
        first = self.registry.path.read_bytes()
        self.registry.sync_history()
        self.assertEqual(self.registry.path.read_bytes(), first)

    def test_latest_corrected_report_wins_over_older_no_show_and_preliminary(self):
        older = no_show_report()
        final = dict(report(cancelled=[], checked=START + timedelta(hours=5)), no_hours=[])
        for name, raw in (("a", final), ("b", older), ("c", dict(older, preliminary=True))):
            write_json(self.root/"reports"/name/"report.json", raw)
        self.registry.sync_history()
        self.assertEqual(self.registry.rows(), [])

    def test_preliminary_import_and_people_added_without_baseline_do_not_count(self):
        for raw in (dict(no_show_report(), preliminary=True),
                    dict(no_show_report(), baseline_mode="user_import"),
                    dict(report(cancelled=[]), no_hours=[], new_no_hours=[dict(PEOPLE[1], hours=0)])):
            self.registry.record(raw)
        self.assertEqual(self.registry.rows(), [])

    def test_invalid_zero_hours_rows_are_rejected(self):
        cases = [dict(PEOPLE[1], hours=1), dict(PEOPLE[1], hours=False), dict(PEOPLE[1], hours="0"),
                 dict(PEOPLE[1], student_id=999, hours=0)]
        for person in cases:
            with self.subTest(person=person), self.assertRaises(DataError):
                self.registry.record(dict(no_show_report(), no_hours=[person]))
        with self.assertRaises(DataError):
            self.registry.record(dict(no_show_report(), attended=[dict(PEOPLE[1], hours=2)]))
        self.assertEqual(self.registry.rows(), [])

    def test_dismissed_old_case_stays_forgiven_during_policy_upgrade(self):
        raw = no_show_report()
        self.registry.record(report())
        self.registry.dismiss(2, self.registry.rows()[0]["revision"])
        state = self.registry.load()
        state["sessions"]["17:101"]["policy_version"] = 1
        write_json(self.registry.path, state)
        write_json(self.root/"reports/latest/report.json", raw)
        self.registry.sync_history()
        self.assertEqual(self.registry.rows(), [])
