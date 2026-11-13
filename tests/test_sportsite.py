from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import sportsite
import api_auth
from test_api_auth import USER_ID, fixture_token, personal_token


START = datetime.fromisoformat("2026-11-05T20:00:00+03:00")
DUE = START - timedelta(hours=1)
EVENT = {
    "title": "Badminton club", "start": START.isoformat(),
    "extendedProps": {"id": 101, "group_id": 17, "can_grade": True},
}
PEOPLE = [
    {"student_id": 1, "full_name": "Тестовый Участник"},
    {"student_id": 2, "full_name": "Вымышленный Игрок"},
]
GRADES = {
    "group_id": 17, "group_name": "Badminton club", "start": START.isoformat(),
    "academic_duration": 2,
    "grades": [dict(person, med_group="do-not-export", email="fictional@example.invalid", hours=0)
               for person in PEOPLE],
}
DETAIL = {"training": {"id": 101, "group": {"id": 17}, "start": START.isoformat(), "load": 2}}


class TemporaryProject(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw_config = json.loads((sportsite.ROOT / "config.example.json").read_text())
        self.raw_config["title_pattern"] = "badminton|бадминтон"
        self.raw_config["group_ids"] = []
        self.raw_config["vpn_control"] = {"enabled": False}
        (self.root / "config.json").write_text(json.dumps(self.raw_config))
        self.config = sportsite.Config.load(self.root / "config.json")

    def reload_config(self):
        (self.root / "config.json").write_text(json.dumps(self.raw_config))
        return sportsite.Config.load(self.root / "config.json")


class ScheduleTests(TemporaryProject):
    def test_requested_days_and_one_hour_offset(self):
        now = datetime.fromisoformat("2026-11-04T21:00:00+03:00")
        starts = self.config.upcoming(now, 5)
        self.assertEqual([s.isoformat() for s in starts], [
            "2026-11-05T20:00:00+03:00", "2026-11-07T18:30:00+03:00",
            "2026-11-08T18:00:00+03:00",
        ])
        self.assertEqual([self.config.due_at(s).strftime("%H:%M") for s in starts],
                         ["19:00", "17:30", "17:00"])

    def test_group_ids_override_title_and_naive_dates_are_rejected(self):
        self.raw_config["group_ids"] = [17]
        config = self.reload_config()
        self.assertTrue(config.matches(dict(EVENT, title="Renamed club")))
        with self.assertRaises(sportsite.DataError):
            sportsite.parse_datetime("2026-11-05T20:00:00")

    def test_invalid_configuration_fails_early(self):
        for field, value in [("poll_seconds", 0), ("max_lateness_seconds", 3600),
                             ("group_ids", [True]), ("title_pattern", "[")]:
            with self.subTest(field=field):
                raw = deepcopy(self.raw_config)
                raw[field] = value
                (self.root / "invalid.json").write_text(json.dumps(raw))
                with self.assertRaises(sportsite.SportError):
                    sportsite.Config.load(self.root / "invalid.json")

    def test_reminder_settings_validate_and_old_configs_keep_defaults(self):
        for key, value in (("training_duration_minutes", 0), ("training_duration_minutes", True),
                           ("reminder_delay_minutes", -1), ("reminder_delay_minutes", "15")):
            with self.subTest(key=key, value=value):
                raw = dict(self.raw_config, **{key: value})
                sportsite.write_json(self.root / "invalid.json", raw)
                with self.assertRaises(sportsite.SportError):
                    sportsite.Config.load(self.root / "invalid.json")
        self.raw_config.pop("training_duration_minutes")
        self.raw_config.pop("reminder_delay_minutes")
        config = self.reload_config()
        self.assertEqual(config.training_duration_minutes, 90)
        self.assertEqual(config.reminder_delay_minutes, 15)

    def test_only_sport_session_is_saved_with_private_permissions(self):
        def cookie(name, domain):
            return {"name": name, "value": "fictional", "domain": domain, "path": "/"}
        sportsite.save_session(self.config.auth_path, {"cookies": [
            cookie("sessionid", sportsite.HOST), cookie("csrftoken", sportsite.HOST),
            cookie("sessionid", "sso.example.invalid"), cookie("sso-cookie", sportsite.HOST),
        ], "origins": [{"origin": "https://sso.example.invalid", "localStorage": []}]})
        saved = json.loads(self.config.auth_path.read_text())
        self.assertEqual(len(saved["cookies"]), 1)
        self.assertEqual(saved["origins"], [])
        self.assertEqual(self.config.auth_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config.auth_path.parent.stat().st_mode & 0o777, 0o700)

    def test_login_keeps_session_if_network_fails_after_manual_sign_in(self):
        with mock.patch("playwright.sync_api.sync_playwright") as playwright, \
                mock.patch("builtins.input", return_value=""), \
                mock.patch("builtins.print"), mock.patch.object(sportsite, "SportAPI") as api:
            browser = playwright.return_value.__enter__.return_value.chromium.launch.return_value
            browser.new_context.return_value.storage_state.return_value = {
                "cookies": [{"name": "sessionid", "value": "fictional-session",
                             "domain": sportsite.HOST, "path": "/"}], "origins": []}
            api.return_value.calendar.side_effect = sportsite.SportError("fictional network timeout")
            with self.assertRaises(sportsite.SportError):
                sportsite.login(self.config)
            self.assertTrue(self.config.auth_path.exists())
            browser.close.assert_called_once()

    def test_loading_session_checks_website_and_expiry_without_network(self):
        cookie = {"name": "sessionid", "value": "fictional-session", "domain": sportsite.HOST,
                  "path": "/", "expires": -1}
        sportsite.save_session(self.config.auth_path, {"cookies": [cookie]})
        self.assertEqual(sportsite.load_session(self.config)["cookies"][0]["name"], "sessionid")
        cookie["expires"] = 1
        sportsite.save_session(self.config.auth_path, {"cookies": [cookie]})
        with self.assertRaises(sportsite.AuthError):
            sportsite.load_session(self.config)
        cookie["expires"] = -1
        cookie["domain"] = "sso.example.invalid"
        sportsite.write_json(self.config.auth_path, {"cookies": [cookie]})
        with self.assertRaises(sportsite.AuthError):
            sportsite.load_session(self.config)

    def test_deployment_keeps_private_session_and_config_outside_source_folder(self):
        sportsite.save_session(self.config.auth_path, {"cookies": [{
            "name": "sessionid", "value": "fictional-session", "domain": sportsite.HOST,
            "path": "/", "expires": -1}]})
        destination = self.root / "installed-copy"
        with mock.patch.object(sportsite, "INSTALL_ROOT", destination), \
                mock.patch.object(sportsite.subprocess, "run", return_value=mock.Mock(returncode=0)):
            deployed = sportsite.deploy_runtime(self.config, self.root / "config.json")
        self.assertEqual(deployed.root, destination.resolve())
        self.assertTrue((destination / "sportsite.py").is_file())
        self.assertTrue((destination / "violation_registry.py").is_file())
        self.assertTrue((destination / "bot_notifications.py").is_file())
        self.assertTrue((destination / "i18n.py").is_file())
        self.assertEqual(json.loads(deployed.auth_path.read_text()),
                         json.loads(self.config.auth_path.read_text()))
        self.assertEqual(deployed.auth_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(deployed.group_ids, self.config.group_ids)

    def test_default_commands_follow_installed_config_marker(self):
        installed = self.root / "installed-copy" / "config.json"
        sportsite.atomic_write(installed, "{}")
        sportsite.write_json(self.root / ".state" / "installation.json", {"config_path": str(installed)})
        with mock.patch.object(sportsite, "ROOT", self.root):
            self.assertEqual(sportsite.default_config_path(), installed)


@contextmanager
def fake_connection(config):
    class FakeAPI:
        def find(self, start):
            return deepcopy(EVENT)

        def roster(self, event, start):
            return deepcopy(PEOPLE)
    yield FakeAPI()


class ExportTests(TemporaryProject):
    def capture(self, scheduled=True, at=DUE):
        return sportsite.collect(self.config, START, scheduled=scheduled,
                                 clock=lambda: at, connector=fake_connection)

    def test_export_contains_names_and_timing_and_is_immutable(self):
        directory = self.capture()
        original = (directory / "snapshot.json").read_text()
        snapshot = json.loads(original)
        self.assertEqual(snapshot["participant_count"], 2)
        self.assertEqual(snapshot["captured_at"], DUE.isoformat())
        self.assertEqual(snapshot["training_start"], START.isoformat())
        self.assertEqual(snapshot["training_end"], (START + timedelta(minutes=90)).isoformat())
        self.assertEqual(snapshot["delay_seconds"], 0)
        self.assertIn("Тестовый Участник", (directory / "participants.csv").read_text())
        self.assertEqual((directory / "snapshot.json").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(sportsite.DataError):
            self.capture()
        self.assertEqual((directory / "snapshot.json").read_text(), original)

    def test_manual_export_is_separate_and_does_not_replace_scheduled(self):
        manual = self.capture(scheduled=False, at=DUE - timedelta(minutes=20))
        scheduled = self.capture()
        self.assertNotEqual(manual, scheduled)
        self.assertIn("_manual_", manual.name)
        self.assertEqual(json.loads((manual / "snapshot.json").read_text())["mode"], "manual")

    def test_calendar_end_is_saved_for_post_training_reminder(self):
        @contextmanager
        def connector(config):
            api = mock.Mock()
            api.find.return_value = dict(EVENT, end=(START + timedelta(minutes=60)).isoformat())
            api.roster.return_value = deepcopy(PEOPLE)
            yield api
        directory = sportsite.collect(self.config, START, True, clock=lambda: DUE, connector=connector)
        self.assertEqual(json.loads((directory / "snapshot.json").read_text())["training_end"],
                         (START + timedelta(minutes=60)).isoformat())

    def test_never_backdates_an_early_late_or_started_training(self):
        for at in (DUE - timedelta(seconds=1), DUE + timedelta(seconds=121), START):
            with self.subTest(at=at), self.assertRaises(sportsite.TooLateError):
                self.capture(at=at)
        self.assertFalse((self.root / "exports").exists())

    def test_response_finishing_after_deadline_is_not_saved(self):
        clock = mock.Mock(side_effect=[DUE, DUE + timedelta(seconds=121)])
        with self.assertRaises(sportsite.TooLateError):
            sportsite.collect(self.config, START, True, clock=clock, connector=fake_connection)
        self.assertFalse((self.root / "exports").exists())

    def test_partial_write_does_not_block_a_retry(self):
        real_write = sportsite.write_json
        with mock.patch.object(sportsite, "write_json", side_effect=OSError("fictional disk failure")):
            with self.assertRaises(OSError):
                self.capture()
        self.assertEqual(list((self.root / "exports").iterdir()), [])
        with mock.patch.object(sportsite, "write_json", wraps=real_write):
            self.assertTrue((self.capture() / "snapshot.json").exists())

    def test_csv_formula_names_are_escaped(self):
        snapshot = {"participants": [{"full_name": " =1+1", "student_id": 1}],
                    "training_start": "test", "captured_at": "test", "mode": "manual"}
        rows = list(csv.reader(StringIO(sportsite.csv_text(snapshot)), delimiter=";"))
        self.assertEqual(rows[1][1], "' =1+1")


class SchedulerTests(TemporaryProject):
    def test_temporary_network_failure_at_start_does_not_disable_scheduler(self):
        with mock.patch.object(sportsite, "check", side_effect=sportsite.SportError("fictional timeout")), \
                mock.patch.object(sportsite, "show_schedule"), \
                mock.patch.object(sportsite, "Scheduler") as scheduler, \
                mock.patch.object(sportsite.time, "sleep", side_effect=KeyboardInterrupt), \
                self.assertLogs("sportsite", "WARNING"), self.assertRaises(KeyboardInterrupt):
            try:
                sportsite.run(self.config, once=False)
            finally:
                scheduler.return_value.tick.assert_called_once()

    def test_missing_authentication_still_stops_startup(self):
        with mock.patch.object(sportsite, "check", side_effect=sportsite.AuthError("fictional expiry")), \
                mock.patch.object(sportsite, "Scheduler") as scheduler, self.assertRaises(sportsite.AuthError):
            try:
                sportsite.run(self.config, once=False)
            finally:
                scheduler.return_value.tick.assert_not_called()

    def test_exact_deadline_is_inclusive_and_job_runs_once_across_restart(self):
        collector = mock.Mock(return_value=self.root / "a-snapshot")
        scheduler = sportsite.Scheduler(self.config, collector)
        scheduler.tick(DUE - timedelta(seconds=1))
        collector.assert_not_called()
        scheduler.tick(DUE)
        collector.assert_called_once_with(self.config, START, scheduled=True)
        sportsite.Scheduler(self.config, collector).tick(DUE + timedelta(seconds=20))
        self.assertEqual(collector.call_count, 1)

    def test_retry_then_success(self):
        collector = mock.Mock(side_effect=[sportsite.AuthError("fictional expiry"), self.root / "snapshot"])
        scheduler = sportsite.Scheduler(self.config, collector)
        with self.assertLogs("sportsite", "ERROR"):
            self.assertEqual(scheduler.tick(DUE), 1)
        self.assertEqual(scheduler.tick(DUE + timedelta(seconds=10)), 0)
        self.assertEqual(collector.call_count, 2)

    def test_sleep_or_shutdown_records_missed_instead_of_late_snapshot(self):
        collector = mock.Mock()
        with self.assertLogs("sportsite", "ERROR"):
            sportsite.Scheduler(self.config, collector).tick(DUE + timedelta(minutes=10))
        collector.assert_not_called()
        jobs = json.loads((self.root / ".state" / "jobs.json").read_text())
        self.assertEqual(jobs["2026-11-05_20-00"]["status"], "missed")

    def test_restart_recovers_export_completed_before_journal_write(self):
        sportsite.collect(self.config, START, True, clock=lambda: DUE, connector=fake_connection)
        collector = mock.Mock()
        sportsite.Scheduler(self.config, collector).tick(DUE + timedelta(seconds=15))
        collector.assert_not_called()

    def test_manual_snapshot_never_counts_as_scheduled(self):
        sportsite.collect(self.config, START, False, clock=lambda: DUE, connector=fake_connection)
        collector = mock.Mock(return_value=self.root / "snapshot")
        sportsite.Scheduler(self.config, collector).tick(DUE)
        collector.assert_called_once()


class APITests(TemporaryProject):
    def setUp(self):
        super().setUp()
        self.routes = {
            "/api/calendar/trainings": (200, [deepcopy(EVENT)]),
            "/api/attendance/101/grades": (200, deepcopy(GRADES)),
            "/api/training/101": (200, deepcopy(DETAIL)),
        }
        self.calls = []
        routes, calls = self.routes, self.calls

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append((self.path, self.headers.get("Cookie"), self.headers.get("Authorization")))
                status, data = routes.get(urlsplit(self.path).path, (404, {}))
                self.send_response(status)
                self.send_header("Content-Type", "application/json" if not isinstance(data, str) else "text/html")
                if status == 302:
                    self.send_header("Location", "/sso-not-to-be-followed")
                self.end_headers()
                self.wfile.write((data if isinstance(data, str) else json.dumps(data)).encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        origin = "http://127.0.0.1:%s" % self.server.server_address[1]
        self.patch = mock.patch.object(sportsite, "ORIGIN", origin)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.request = self.playwright.request.new_context(storage_state={"cookies": [{
            "name": "sessionid", "value": "fictional-session", "domain": "127.0.0.1",
            "path": "/", "expires": -1, "httpOnly": True, "secure": False, "sameSite": "Lax",
        }], "origins": []})
        self.addCleanup(self.request.dispose)
        self.api = sportsite.SportAPI(self.request, self.config)

    def test_real_http_cookie_query_and_sanitized_roster(self):
        event = self.api.find(START)
        people = self.api.roster(event, START)
        self.assertEqual({p["student_id"] for p in people}, {1, 2})
        self.assertTrue(all(set(p) == {"full_name", "student_id"} for p in people))
        self.assertTrue(all("sessionid=fictional-session" == call[1] for call in self.calls))
        query = parse_qs(urlsplit(self.calls[0][0]).query)
        self.assertEqual(query["start"], ["2026-11-05T00:00:00+03:00"])

    def test_utc_event_time_matches_moscow_slot(self):
        self.routes["/api/calendar/trainings"][1][0]["start"] = "2026-11-05T17:00:00Z"
        self.assertEqual(self.api.find(START)["extendedProps"]["id"], 101)

    def test_expiry_permission_and_html_login_are_not_empty_rosters(self):
        for status, body, error in [
            (401, {}, sportsite.AuthError),
            (403, {"detail": "You are not a teacher of this group"}, sportsite.PermissionError),
            (403, {"detail": "Authentication credentials were not provided."}, sportsite.AuthError),
            (200, "<html>SSO login</html>", sportsite.AuthError),
            (302, {}, sportsite.AuthError),
            (500, {}, sportsite.SportError),
        ]:
            with self.subTest(status=status, body=body):
                self.routes["/api/attendance/101/grades"] = (status, body)
                with self.assertRaises(error):
                    self.api.roster(EVENT, START)
        self.assertFalse(any("sso-not-to-be-followed" in call[0] for call in self.calls))

    def test_ambiguous_and_missing_training_are_errors(self):
        self.routes["/api/calendar/trainings"] = (200, [EVENT, dict(EVENT)])
        with self.assertRaises(sportsite.DataError):
            self.api.find(START)
        self.routes["/api/calendar/trainings"] = (200, [])
        with self.assertRaises(sportsite.DataError):
            self.api.find(START)

    def test_wrong_group_or_count_is_rejected(self):
        self.routes["/api/attendance/101/grades"][1]["group_id"] = 99
        with self.assertRaises(sportsite.DataError):
            self.api.roster(EVENT, START)
        self.routes["/api/attendance/101/grades"] = (200, deepcopy(GRADES))
        self.routes["/api/training/101"][1]["training"]["load"] = 1
        with self.assertRaises(sportsite.DataError):
            self.api.roster(EVENT, START)

    def test_true_empty_roster_is_valid_but_missing_grades_is_not(self):
        self.routes["/api/attendance/101/grades"][1]["grades"] = []
        self.routes["/api/training/101"][1]["training"]["load"] = 0
        self.assertEqual(self.api.roster(EVENT, START), [])
        del self.routes["/api/attendance/101/grades"][1]["grades"]
        with self.assertRaises(sportsite.DataError):
            self.api.roster(EVENT, START)

    def test_complete_export_against_actual_http_responses(self):
        @contextmanager
        def connector(config):
            yield self.api
        directory = sportsite.collect(self.config, START, True, clock=lambda: DUE, connector=connector)
        text = (directory / "snapshot.json").read_text()
        self.assertIn("Вымышленный Игрок", text)
        for forbidden in ("do-not-export", "fictional@example.invalid", "hours", "med_group", "sessionid"):
            self.assertNotIn(forbidden, text)

    def test_scheduled_snapshot_to_telegram_report_over_real_http(self):
        import attendance_report
        import telegram_bot
        @contextmanager
        def connector(config):
            yield self.api
        snapshot_dir = sportsite.collect(self.config, START, True, clock=lambda: DUE, connector=connector)
        original = (snapshot_dir / "snapshot.json").read_bytes()
        # One original participant cancelled, another received hours, one arrived later.
        current = deepcopy(GRADES)
        current["grades"] = [dict(PEOPLE[0], hours=2),
                             {"student_id": 3, "full_name": "Поздний Вымышленный", "hours": 1}]
        self.routes["/api/attendance/101/grades"] = (200, current)
        self.routes["/api/training/101"][1]["training"]["end"] = (START + timedelta(hours=2)).isoformat()
        report = attendance_report.build_report(self.config, START.date(), connector=connector,
                                               clock=lambda: START + timedelta(hours=3))
        self.assertEqual([p["student_id"] for p in report["cancelled"]], [2])
        self.assertEqual({p["student_id"] for p in report["attended"]}, {1, 3})
        self.assertEqual(report["no_hours"], [])
        api = mock.Mock()
        settings = {"chat_id": 77, "user_id": 77, "username": "fictional_bot"}
        update = {"update_id": 1, "message": {"chat": {"id": 77, "type": "private"},
                  "from": {"id": 77, "is_bot": False}, "date": 100, "text": "/check 2026-11-05"}}
        telegram_bot.handle_update(self.config, settings, api, update, now=100,
                                  reporter=lambda config, day, language="ru": attendance_report.text_report(report, language))
        sent = api.send.call_args.args[1]
        self.assertIn("Вымышленный Игрок", sent)
        self.assertIn("2 ч", sent)
        self.assertIn("не было в снимке", sent)
        self.assertEqual((snapshot_dir / "snapshot.json").read_bytes(), original)

    def test_api_mode_works_without_cookies_and_keeps_keys_on_their_own_hosts(self):
        import time
        service = personal_token()
        short = fixture_token({"aud": "sport", "email": "fictional@example.invalid", "exp": time.time() + 86400})
        self.routes["/tokens/generate-sport-token"] = (200, {"access_token": short})
        sportsite.write_json(self.config.api_token_path, {"access_token": service})
        self.assertFalse(self.config.auth_path.exists())
        with mock.patch.object(api_auth, "ACCOUNTS_ORIGIN", sportsite.ORIGIN), \
                mock.patch("playwright.sync_api.sync_playwright") as factory:
            factory.return_value.__enter__.return_value = self.playwright
            for _ in range(2):
                with sportsite.connect(self.config) as api:
                    people = api.roster(api.find(START), START)
                    self.assertEqual(len(people), 2)
        exchanges = [call for call in self.calls if call[0].startswith("/tokens/")]
        self.assertEqual(len(exchanges), 2)
        for path, cookie, authorization in self.calls:
            self.assertIsNone(cookie)
            if path.startswith("/tokens/"):
                self.assertEqual(authorization, "Bearer " + service)
                self.assertEqual(parse_qs(urlsplit(path).query)["innohassle_id"], [USER_ID])
            else:
                self.assertEqual(authorization, "Bearer " + short)

    def test_api_exchange_error_does_not_silently_use_cookie_session(self):
        self.routes["/tokens/generate-sport-token"] = (403, {})
        sportsite.write_json(self.config.api_token_path, {"access_token": personal_token()})
        with mock.patch.object(api_auth, "ACCOUNTS_ORIGIN", sportsite.ORIGIN), \
                mock.patch("playwright.sync_api.sync_playwright") as factory, self.assertRaises(sportsite.AuthError):
            factory.return_value.__enter__.return_value = self.playwright
            with sportsite.connect(self.config):
                self.fail("Invalid credentials must not reach the sport API")
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
