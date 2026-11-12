from datetime import timedelta
import json
from unittest import mock

import sportsite
from bot_notifications import NotificationError, ServiceNotifications
from test_sportsite import TemporaryProject, START, DUE, fake_connection
from test_violation_registry import report


class ServiceNotificationTests(TemporaryProject):
    def setUp(self):
        super().setUp()
        self.at = DUE - timedelta(minutes=1)
        clock = mock.patch.object(sportsite.Config, "now", lambda config: self.at)
        clock.start()
        self.addCleanup(clock.stop)
        self.api = mock.Mock()
        self.settings = {"chat_id": 77}
        self.worker = ServiceNotifications(self.config, self.settings, self.api)
        self.worker.load_state()
        self.snapshot = sportsite.collect(self.config, START, True, clock=lambda: DUE, connector=fake_connection)
        self.jobs_path = self.root / ".state/jobs.json"
        self.jobs({"status": "saved", "path": str(self.snapshot)})

    def jobs(self, value):
        sportsite.write_json(self.jobs_path, {self.config.job_key(START): value})

    def test_reminder_after_end_plus_delay_once_across_restart(self):
        self.at = START + timedelta(minutes=104)
        self.assertEqual(self.worker.tick(), 0)
        self.at += timedelta(minutes=1)
        self.assertEqual(self.worker.tick(), 1)
        self.assertIn("Сохрани посещаемость", self.api.send.call_args.args[1])
        self.assertIn("/check 2026-11-05", self.api.send.call_args.args[1])
        self.assertEqual(ServiceNotifications(self.config, self.settings, self.api).tick(), 0)
        self.api.send.assert_called_once()
        self.assertEqual(self.worker.state_path.stat().st_mode & 0o777, 0o600)

    def test_calendar_end_overrides_fallback_duration(self):
        path = self.snapshot / "snapshot.json"
        raw = json.loads(path.read_text())
        raw["training_end"] = (START + timedelta(hours=2)).isoformat()
        sportsite.write_json(path, raw)
        self.at = START + timedelta(minutes=105)
        self.assertEqual(self.worker.tick(), 0)
        self.at = START + timedelta(minutes=135)
        self.assertEqual(self.worker.tick(), 1)

    def test_final_check_suppresses_reminder_but_preliminary_does_not(self):
        raw = dict(report(), title="Badminton club", training_end=(START + timedelta(minutes=90)).isoformat(),
                   checked_at=(START + timedelta(minutes=95)).isoformat())
        path = self.root / "reports" / (self.config.job_key(START) + "_training-101_check_1") / "report.json"
        sportsite.write_json(path, raw)
        self.at = START + timedelta(minutes=105)
        self.assertEqual(self.worker.tick(), 0)
        self.assertEqual(self.worker.load_state()["delivered"]["reminder:" + self.config.job_key(START)]["status"], "checked")
        self.api.send.assert_not_called()
        # A preliminary-only check must still get a reminder.
        self.worker.state_path.unlink()
        self.at = DUE - timedelta(minutes=1)
        self.worker.load_state()
        sportsite.write_json(path, dict(raw, preliminary=True))
        self.at = START + timedelta(minutes=105)
        self.assertEqual(self.worker.tick(), 1)

    def test_historical_or_long_expired_reminders_do_not_flood(self):
        self.at = START + timedelta(hours=5)
        self.assertEqual(self.worker.tick(), 0)
        self.worker.state_path.unlink()
        self.worker.load_state()
        self.assertEqual(self.worker.tick(), 0)
        self.api.send.assert_not_called()

    def test_failed_delivery_is_retried_and_only_checkpointed_on_success(self):
        self.at = START + timedelta(minutes=105)
        self.api.send.side_effect = [sportsite.SportError("fictional outage"), None]
        with self.assertRaises(sportsite.SportError):
            self.worker.tick()
        self.assertNotIn("reminder:" + self.config.job_key(START), self.worker.load_state()["delivered"])
        self.assertEqual(ServiceNotifications(self.config, self.settings, self.api).tick(), 1)

    def test_auth_export_error_sends_sanitized_notice_once_then_missed_alert(self):
        (self.snapshot / "snapshot.json").unlink()
        self.jobs({"status": "retry", "error_kind": "auth", "error": "fictional-secret-do-not-forward"})
        self.at = DUE
        self.assertEqual(self.worker.tick(), 1)
        message = self.api.send.call_args.args[1]
        self.assertIn("Сессия", message)
        self.assertIn("login.command", message)
        self.assertNotIn("fictional-secret", message)
        self.assertEqual(self.worker.tick(), 0)
        self.at += timedelta(seconds=181)
        self.assertEqual(self.worker.tick(), 1)
        self.assertIn("пропущен", self.api.send.call_args.args[1])
        self.assertEqual(self.worker.tick(), 0)

    def test_missing_deadline_is_detected_even_when_scheduler_never_started(self):
        (self.snapshot / "snapshot.json").unlink()
        sportsite.write_json(self.jobs_path, {})
        self.at = DUE + timedelta(seconds=181)
        self.assertEqual(self.worker.tick(), 1)
        self.assertIn("пропущен", self.api.send.call_args.args[1])

    def test_completed_export_prevents_false_missed_alarm(self):
        self.jobs({"status": "missed", "recorded_at": DUE.isoformat()})
        self.at = DUE + timedelta(seconds=181)
        self.assertEqual(self.worker.tick(), 0)
        self.api.send.assert_not_called()

    def test_startup_failure_deduplicates_until_recovery_then_can_alert_again(self):
        self.at = DUE
        sportsite.worker_health(self.config, self.at, sportsite.AuthError("fictional-secret"))
        self.assertEqual(self.worker.tick(), 1)
        self.assertNotIn("fictional-secret", self.api.send.call_args.args[1])
        self.assertEqual(self.worker.tick(), 0)
        sportsite.worker_health(self.config, self.at)
        self.assertEqual(self.worker.tick(), 0)
        sportsite.worker_health(self.config, self.at, sportsite.AuthError("expired"))
        self.assertEqual(self.worker.tick(), 1)

    def test_corrupt_notification_state_fails_closed(self):
        sportsite.atomic_write(self.worker.state_path, "bad json")
        with self.assertRaises(NotificationError):
            self.worker.tick()
        self.assertEqual(self.worker.state_path.read_text(), "bad json")
        self.api.send.assert_not_called()

    def test_other_recipient_state_is_not_reused(self):
        state = self.worker.load_state()
        state["chat_id"] = 88
        sportsite.write_json(self.worker.state_path, state)
        with self.assertRaises(NotificationError):
            self.worker.tick()
        self.api.send.assert_not_called()

    def test_corrupt_jobs_warns_once_and_recovers_without_stopping_commands(self):
        self.at = DUE
        sportsite.atomic_write(self.jobs_path, "bad json")
        self.assertEqual(self.worker.tick(), 1)
        self.assertIn("журнал выгрузок", self.api.send.call_args.args[1])
        self.assertEqual(self.worker.tick(), 0)
        self.jobs({"status": "saved"})
        self.assertEqual(self.worker.tick(), 0)
