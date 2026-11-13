"""Language selection, data preservation and notification delivery regressions."""

import ast
from copy import deepcopy
from datetime import timedelta
import json
import re
from unittest import mock

import attendance_report
import bot_notifications
import i18n
import sportsite
import telegram_bot as bot
from test_sportsite import TemporaryProject, START, DUE, PEOPLE, fake_connection
from test_telegram_bot import SETTINGS, NOW, update
from test_violation_registry import report
from violation_registry import ViolationRegistry


class LanguageTests(TemporaryProject):
    def setUp(self):
        super().setUp()
        self.settings = dict(SETTINGS)
        self.api = mock.Mock()
        sportsite.write_json(bot.settings_path(self.config), self.settings)

    def handle(self, text, **kwargs):
        bot.handle_update(self.config, self.settings, self.api, update(text=text, **kwargs), now=NOW)

    def callback(self, language="en", user=77, chat=77, bot_id=123456789, kind="private"):
        bot.handle_update(self.config, self.settings, self.api, {"callback_query": {
            "id": "language-click", "data": "language:" + language,
            "from": {"id": user, "is_bot": False},
            "message": {"message_id": 20, "from": {"id": bot_id},
                        "chat": {"id": chat, "type": kind}}}}, now=NOW)

    def test_old_profile_defaults_to_russian_and_invalid_language_fails_closed(self):
        self.assertEqual(bot.load_settings(self.config), SETTINGS)
        self.assertEqual(i18n.language_of(self.settings), "ru")
        for invalid in (None, [], {}, 1, "fr"):
            sportsite.write_json(bot.settings_path(self.config), dict(SETTINGS, language=invalid))
            with self.assertRaises(bot.TelegramError):
                bot.load_settings(self.config)

    def test_picker_and_direct_switch_persist_across_restart(self):
        self.handle("/language")
        markup = self.api.send.call_args.kwargs["reply_markup"]
        self.assertEqual([b["callback_data"] for b in markup["inline_keyboard"][0]],
                         ["language:ru", "language:en"])
        self.handle("/language en")
        self.assertEqual(self.settings["language"], "en")
        reloaded = bot.load_settings(self.config)
        self.assertEqual(reloaded["language"], "en")
        self.assertEqual({k: v for k, v in reloaded.items() if k != "language"}, SETTINGS)
        self.assertIn("Language switched to English", self.api.send.call_args.args[1])
        commands = self.api.call.call_args.args[1]["commands"]
        self.assertIn("language", [item["command"] for item in commands])
        self.assertNotRegex(json.dumps(commands, ensure_ascii=False), r"[А-Яа-яЁё]")
        bot.handle_update(self.config, reloaded, self.api, update(text="/status"), now=NOW)
        self.assertIn("Next scheduled export", self.api.send.call_args.args[1])
        self.handle("/language ru")
        self.assertEqual(bot.load_settings(self.config)["language"], "ru")

    def test_only_owner_private_commands_and_own_bot_callbacks_can_change_language(self):
        original = bot.settings_path(self.config).read_bytes()
        self.handle("/language en", user=88)
        self.handle("/language en", kind="group")
        self.handle("/language en", timestamp=NOW - 901)
        self.handle("/language@another_bot en")
        for kwargs in ({"user": 88}, {"chat": 88}, {"kind": "group"}, {"bot_id": 9}):
            self.callback(**kwargs)
        self.callback("fr")
        self.assertEqual(bot.settings_path(self.config).read_bytes(), original)
        self.callback()
        self.assertEqual(bot.load_settings(self.config)["language"], "en")

    def test_invalid_arguments_and_expired_callback_never_write_preference(self):
        original = bot.settings_path(self.config).read_bytes()
        for text in ("/language fr", "/language en ru"):
            self.handle(text)
            self.assertIn("Формат", self.api.send.call_args.args[1])
        self.api.call.side_effect = bot.TelegramError("expired callback")
        self.callback()
        self.assertEqual(bot.settings_path(self.config).read_bytes(), original)

    def test_failed_preference_write_preserves_disk_and_shared_settings(self):
        with mock.patch.object(bot, "write_json", side_effect=OSError("disk full")):
            self.handle("/language en")
        self.assertNotIn("language", self.settings)
        self.assertEqual(bot.load_settings(self.config), SETTINGS)
        self.assertIn("Не удалось сохранить язык", self.api.send.call_args.args[1])

    def test_menu_failure_does_not_revert_successfully_saved_preference(self):
        self.api.call.side_effect = bot.TelegramError("offline")
        with self.assertLogs("sportsite", "WARNING"):
            self.handle("/language en")
        self.assertEqual(self.settings["language"], "en")
        self.assertEqual(bot.load_settings(self.config)["language"], "en")

    def test_english_help_usage_checks_and_errors(self):
        self.settings["language"] = "en"
        for text, expected in (("/help", "Badminton"), ("/check 2026-02-30", "Usage:"),
                               ("/violations extra", "without arguments"),
                               ("/undo extra", "without arguments"),
                               ("hello", "Send /check")):
            self.handle(text)
            self.assertIn(expected, self.api.send.call_args.args[1])
        reporter = mock.Mock(side_effect=sportsite.AuthError("Сессия истекла"))
        bot.handle_update(self.config, self.settings, self.api, update(), reporter=reporter, now=NOW)
        reporter.assert_called_once_with(self.config, None, language="en")
        self.assertIn("website session expired", self.api.send.call_args.args[1])
        self.assertNotRegex(i18n.error_text(sportsite.DataError("Неизвестный формат"), "en"), r"[А-Яа-яЁё]")

    def test_register_language_changes_preserve_names_cases_and_button_identity(self):
        registry = ViolationRegistry(self.config)
        registry.record(report())
        original = registry.path.read_bytes()
        rows = registry.rows()
        _, ru = bot.violations_page(self.config, rows)
        text, en = bot.violations_page(self.config, rows, notice="Счётчик: 2 → 1. Списан самый ранний случай.", language="en")
        self.assertIn(PEOPLE[1]["full_name"], text)
        self.assertIn("Counter: 2 → 1", text)
        self.assertIn("Remove all", en["inline_keyboard"][0][1]["text"])
        self.assertEqual([b["callback_data"] for b in ru["inline_keyboard"][0]],
                         [b["callback_data"] for b in en["inline_keyboard"][0]])
        self.handle("/language en")
        self.assertEqual(registry.path.read_bytes(), original)
        self.assertIn("Cases restored: 2", i18n.localize_notice(
            "↩️ Последнее действие отменено. Восстановлено случаев: 2.", "en"))

    def test_complete_english_reports_keep_names_and_data(self):
        raw = dict(report(), snapshot_at=DUE.isoformat(), attended=[], no_hours=[], new_no_hours=[])
        raw["attended"] = [{"student_id": 3, "full_name": "Fictional Guest", "hours": 1, "in_snapshot": False}]
        for key in ("registered", "cancelled"):
            for person in raw[key]:
                person["full_name"] = "Fictional Player " + str(person["student_id"])
        original = deepcopy(raw)
        for imported in (False, True):
            raw["baseline_mode"] = "user_import" if imported else "scheduled"
            raw["reference_at"] = DUE.isoformat()
            text = attendance_report.text_report(raw, "en")
            self.assertNotRegex(text, r"[А-Яа-яЁё]")
            self.assertIn("Fictional Guest — 1 h", text)
        self.assertEqual(raw["registered"], original["registered"])

    def test_catalog_covers_translated_literals_and_alert_categories(self):
        for name in ("telegram_bot.py", "attendance_report.py", "bot_notifications.py"):
            tree = ast.parse((sportsite.ROOT / name).read_text())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_"
                        and node.args and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str) and re.search(r"[А-Яа-яЁё]", node.args[0].value)):
                    self.assertIn(node.args[0].value, i18n.EN, name)
        for text in bot_notifications.ERROR_TEXT.values():
            self.assertIn(text, i18n.EN)


class LanguageNotificationTests(TemporaryProject):
    def setUp(self):
        super().setUp()
        self.settings, self.api = {"chat_id": 77, "language": "en"}, mock.Mock()
        self.at = DUE - timedelta(minutes=1)
        clock = mock.patch.object(sportsite.Config, "now", lambda config: self.at)
        clock.start()
        self.addCleanup(clock.stop)
        self.snapshot = sportsite.collect(self.config, START, True, clock=lambda: DUE, connector=fake_connection)

    def test_english_roster_is_sent_once_and_names_are_unchanged(self):
        worker = bot.SnapshotNotifications(self.config, self.settings, self.api)
        worker.load_state()
        self.at = DUE
        self.assertEqual(worker.tick(), 1)
        self.assertIn("Planning to attend: 2", self.api.send.call_args.args[1])
        for person in PEOPLE:
            self.assertIn(person["full_name"], self.api.send.call_args.args[1])
        self.settings["language"] = "ru"
        self.assertEqual(worker.tick(), 0)
        self.api.send.assert_called_once()

    def test_partial_old_russian_delivery_retains_language_when_switching(self):
        self.settings["language"] = "ru"
        worker = bot.SnapshotNotifications(self.config, self.settings, self.api)
        worker.load_state()
        raw = json.loads((self.snapshot / "snapshot.json").read_text())
        raw["participants"] = [{"student_id": i, "full_name": "Fictional Player " + str(i) + " A" * 80} for i in range(1, 61)]
        raw["participant_count"] = len(raw["participants"])
        sportsite.write_json(self.snapshot / "snapshot.json", raw)
        chunks = bot.message_chunks(bot.snapshot_notification(raw, self.config, "ru"))
        self.assertGreater(len(chunks), 1)
        self.at = DUE
        self.api.send.side_effect = [None, bot.TelegramError("offline")]
        with self.assertRaises(bot.TelegramError):
            worker.tick()
        state = worker.load_state()
        entry = next(iter(state["deliveries"].values()))
        self.assertEqual(entry["parts_sent"], 1)
        entry.pop("language")  # Older installations had no per-delivery language.
        sportsite.write_json(worker.state_path, state)
        self.settings["language"] = "en"
        self.api.reset_mock()
        self.api.send.side_effect = None
        self.assertEqual(worker.tick(), 1)
        self.assertEqual([c.args[1] for c in self.api.send.call_args_list], chunks[1:])
        self.assertEqual(worker.tick(), 0)

    def test_reminders_and_alerts_use_live_preference_without_resending_old_messages(self):
        self.settings["language"] = "ru"
        worker = bot_notifications.ServiceNotifications(self.config, self.settings, self.api)
        worker.load_state()
        jobs_path = self.root / ".state/jobs.json"
        sportsite.write_json(jobs_path, {self.config.job_key(START): {"status": "saved"}})
        self.at = START + timedelta(minutes=105)
        self.settings["language"] = "en"
        self.assertEqual(worker.tick(), 1)
        self.assertIn("Save attendance hours", self.api.send.call_args.args[1])
        self.settings["language"] = "ru"
        self.assertEqual(worker.tick(), 0)
        self.settings["language"] = "en"
        sportsite.worker_health(self.config, self.at, sportsite.AuthError("secret-do-not-forward"))
        self.assertEqual(worker.tick(), 1)
        text = self.api.send.call_args.args[1]
        self.assertIn("session expired", text)
        self.assertNotIn("secret-do-not-forward", text)
        self.assertNotRegex(text, r"[А-Яа-яЁё]")
