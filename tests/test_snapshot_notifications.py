from datetime import timedelta
import json
from unittest import mock

import sportsite
import telegram_bot as bot
from test_sportsite import TemporaryProject, START, DUE, PEOPLE, fake_connection


SETTINGS = {"chat_id": 77}


class NotificationTests(TemporaryProject):
    def setUp(self):
        super().setUp()
        self.api = mock.Mock()
        self.notifier = bot.SnapshotNotifications(self.config, SETTINGS, self.api)
        sportsite.write_json(self.notifier.state_path, {
            "schema_version": 1, "chat_id": 77, "enabled_at": (DUE - timedelta(days=1)).isoformat(),
            "deliveries": {}})

    def capture(self, scheduled=True):
        return sportsite.collect(self.config, START, scheduled, clock=lambda: DUE, connector=fake_connection)

    def test_scheduled_success_contains_exact_people_and_sends_once_across_restart(self):
        directory = self.capture()
        original = (directory / "snapshot.json").read_bytes()
        self.assertEqual(self.notifier.tick(), 1)
        chat, text = self.api.send.call_args.args
        self.assertEqual(chat, 77)
        self.assertIn("успешно сохранён", text)
        self.assertIn("05.11.2026 20:00", text)
        self.assertIn("05.11 19:00:00", text)
        self.assertIn("Планируют прийти: 2", text)
        for person in PEOPLE:
            self.assertIn(person["full_name"], text)
        self.assertEqual(bot.SnapshotNotifications(self.config, SETTINGS, self.api).tick(), 0)
        self.api.send.assert_called_once()
        self.assertEqual((directory / "snapshot.json").read_bytes(), original)
        self.assertEqual(self.notifier.state_path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / ".state/jobs.json").exists())

    def test_manual_imported_and_incomplete_exports_do_not_send_success(self):
        self.capture(scheduled=False)
        sportsite.write_json(self.root / "imports" / self.config.job_key(START) / "baseline.json",
                            {"mode": "user_import"})
        (self.root / "exports/.pending-incomplete").mkdir()
        self.assertEqual(self.notifier.tick(), 0)
        self.api.send.assert_not_called()

    def test_old_snapshots_do_not_create_historical_notification_flood(self):
        self.capture()
        state = self.notifier.load_state()
        state["enabled_at"] = (START + timedelta(days=1)).isoformat()
        sportsite.write_json(self.notifier.state_path, state)
        self.assertEqual(self.notifier.tick(), 0)
        self.api.send.assert_not_called()

    def test_delivery_failure_is_retried_without_recollecting_the_roster(self):
        directory = self.capture()
        self.api.send.side_effect = [bot.TelegramError("fictional network failure"), None]
        with self.assertRaises(bot.TelegramError):
            self.notifier.tick()
        state = self.notifier.load_state()
        entry = next(iter(state["deliveries"].values()))
        self.assertEqual(entry["parts_sent"], 0)
        self.assertNotIn("sent_at", entry)
        self.assertTrue((directory / "snapshot.json").exists())
        self.assertEqual(bot.SnapshotNotifications(self.config, SETTINGS, self.api).tick(), 1)
        self.assertEqual(self.api.send.call_count, 2)

    def test_partial_delivery_resumes_at_unsent_message(self):
        self.capture()
        self.api.send.side_effect = [None, bot.TelegramError("fictional network failure"), None]
        with mock.patch.object(bot, "message_chunks", return_value=["first", "second"]):
            with self.assertRaises(bot.TelegramError):
                self.notifier.tick()
            self.assertEqual(next(iter(self.notifier.load_state()["deliveries"].values()))["parts_sent"], 1)
            self.assertEqual(bot.SnapshotNotifications(self.config, SETTINGS, self.api).tick(), 1)
        self.assertEqual([call.args[1] for call in self.api.send.call_args_list], ["first", "second", "second"])

    def test_corrupted_snapshot_and_wrong_group_never_trigger_false_success(self):
        directory = self.capture()
        path = directory / "snapshot.json"
        raw = json.loads(path.read_text())
        for changes in ({"participant_count": 0}, {"title": "Other sport", "group_id": 999},
                        {"captured_at": (START + timedelta(days=10000)).isoformat()}):
            sportsite.write_json(path, dict(raw, **changes))
            with self.assertLogs("sportsite", "WARNING"):
                self.assertEqual(self.notifier.tick(), 0)
        self.api.send.assert_not_called()

    def test_zero_registered_is_a_real_success_and_not_an_authentication_fallback(self):
        directory = self.capture()
        raw = json.loads((directory / "snapshot.json").read_text())
        raw.update(participants=[], participant_count=0)
        sportsite.write_json(directory / "snapshot.json", raw)
        self.assertEqual(self.notifier.tick(), 1)
        self.assertIn("Планируют прийти: 0", self.api.send.call_args.args[1])
        self.assertIn("Пока никто не записан", self.api.send.call_args.args[1])

    def test_changed_already_notified_snapshot_is_not_resent(self):
        directory = self.capture()
        self.notifier.tick()
        raw = json.loads((directory / "snapshot.json").read_text())
        raw["participants"][0]["full_name"] = "Изменённое имя"
        sportsite.write_json(directory / "snapshot.json", raw)
        with self.assertLogs("sportsite", "ERROR"):
            self.assertEqual(self.notifier.tick(), 0)
        self.api.send.assert_called_once()

    def test_corrupted_or_other_recipient_journal_fails_closed(self):
        for changes in ({"chat_id": 88}, {"enabled_at": None}, {"deliveries": []}):
            state = {"schema_version": 1, "chat_id": 77,
                     "enabled_at": DUE.isoformat(), "deliveries": {}}
            state.update(changes)
            sportsite.write_json(self.notifier.state_path, state)
            with self.assertRaises(bot.TelegramError):
                self.notifier.tick()
        self.api.send.assert_not_called()

    def test_notification_failure_does_not_stop_incoming_commands(self):
        from test_telegram_bot import SETTINGS as owner, update
        sportsite.write_json(bot.settings_path(self.config), owner)
        api = mock.Mock()
        api.updates.side_effect = [[update(text="/help")], KeyboardInterrupt]
        with mock.patch.object(bot, "TelegramAPI", return_value=api), \
                mock.patch.object(bot, "SnapshotNotifications") as worker, \
                mock.patch.object(bot.time, "time", return_value=1800000000), \
                self.assertLogs("sportsite", "WARNING"), self.assertRaises(KeyboardInterrupt):
            worker.return_value.tick.side_effect = bot.TelegramError("fictional outage")
            bot.serve(self.config)
        api.send.assert_called_once_with(77, bot.HELP)
