from datetime import date
from io import BytesIO, StringIO
import json
import subprocess
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

import sportsite
import telegram_bot as bot
from test_sportsite import TemporaryProject
from test_violation_registry import report
from violation_registry import ViolationRegistry


TOKEN = "123456789:" + "fictional_" * 4
SETTINGS = {"schema_version": 1, "token": TOKEN, "bot_id": 123456789,
            "username": "fictional_badminton_bot", "user_id": 77, "chat_id": 77}
NOW = 1800000000


def update(ident=1, text="/check", user=77, chat=77, kind="private", timestamp=NOW):
    return {"update_id": ident, "message": {"chat": {"id": chat, "type": kind},
            "from": {"id": user, "is_bot": False}, "text": text, "date": timestamp}}


class BotCommandTests(TemporaryProject):
    def handle(self, value, reporter=None):
        api = mock.Mock()
        reporter = reporter or mock.Mock(return_value="Вымышленный отчёт")
        bot.handle_update(self.config, SETTINGS, api, value, reporter=reporter, now=NOW)
        return api, reporter

    def test_private_owner_check_and_explicit_date(self):
        for text, expected in (("/check", None), ("/check 2026-11-07", date(2026, 11, 7)),
                               ("/check@fictional_badminton_bot", None)):
            api, reporter = self.handle(update(text=text))
            reporter.assert_called_once_with(self.config, expected)
            self.assertEqual(api.send.call_args_list[-1].args, (77, "Вымышленный отчёт"))

    def test_other_users_groups_spoofed_sender_and_other_bot_mentions_are_silent(self):
        values = [update(user=88, chat=88), update(user=88), update(chat=-11, kind="group"),
                  update(kind="supergroup"), update(text="/check@another_bot")]
        for value in values:
            with self.subTest(value=value):
                api, reporter = self.handle(value)
                reporter.assert_not_called()
                api.send.assert_not_called()

    def test_invalid_dates_and_stale_commands_do_not_read_sports_site(self):
        for text in ("/check 2026-02-30", "/check 07.11.2026", "/check a b"):
            api, reporter = self.handle(update(text=text))
            reporter.assert_not_called()
            self.assertIn("Формат", api.send.call_args.args[1])
        api, reporter = self.handle(update(timestamp=NOW - 901))
        reporter.assert_not_called()
        self.assertIn("заново", api.send.call_args.args[1])

    def test_site_failure_is_reported_without_making_up_absences(self):
        reporter = mock.Mock(side_effect=sportsite.AuthError("Сессия истекла"))
        api, _ = self.handle(update(), reporter)
        self.assertEqual(api.send.call_args.args[1], "⚠️ Сессия истекла")

    def test_status_and_help_never_read_the_sports_site(self):
        for command in ("/status", "/help", "/start"):
            api, reporter = self.handle(update(text=command))
            reporter.assert_not_called()
            api.send.assert_called_once()


class TelegramTransportTests(unittest.TestCase):
    def test_post_body_and_long_poll_limits(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"ok": True, "result": []}).encode()
        opener = mock.Mock()
        opener.open.return_value = response
        api = bot.TelegramAPI(TOKEN, opener)
        self.assertEqual(api.updates(15), [])
        request = opener.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["offset"], 15)
        self.assertEqual(payload["timeout"], 25)
        self.assertEqual(payload["allowed_updates"], ["message", "callback_query"])
        self.assertEqual(request.method, "POST")

    def test_network_http_and_bad_json_errors_do_not_reveal_token_or_response(self):
        errors = [URLError("https://api.telegram.org/bot" + TOKEN),
                  HTTPError("https://api.telegram.org/bot" + TOKEN, 409, TOKEN, {}, BytesIO(TOKEN.encode()))]
        for error in errors:
            opener = mock.Mock()
            opener.open.side_effect = error
            with self.assertRaises(bot.TelegramError) as caught:
                bot.TelegramAPI(TOKEN, opener).call("getMe")
            self.assertNotIn(TOKEN, str(caught.exception))
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = TOKEN.encode()
        opener = mock.Mock()
        opener.open.return_value = response
        with self.assertRaises(bot.TelegramError) as caught:
            bot.TelegramAPI(TOKEN, opener).call("getMe")
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_redirect_is_not_followed(self):
        self.assertIsNone(bot.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.invalid"))

    def test_long_unicode_report_is_split_without_oversized_messages_or_markup(self):
        text = "\n".join("🏸 <Тест & Игрок> " + str(i) for i in range(1000))
        chunks = bot.message_chunks(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c.encode("utf-16-le")) // 2 <= 3500 for c in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), text.replace("\n", ""))
        api = bot.TelegramAPI(TOKEN)
        api.call = mock.Mock()
        api.send(77, text)
        self.assertEqual(api.call.call_count, len(chunks))
        self.assertNotIn("parse_mode", api.call.call_args.args[1])

    def test_keyboard_is_attached_only_to_last_chunk(self):
        api = bot.TelegramAPI(TOKEN)
        api.call = mock.Mock()
        markup = {"inline_keyboard": [[{"text": "Example", "callback_data": "vr:page:0"}]]}
        api.send(77, "a" * 7001, reply_markup=markup)
        self.assertEqual(api.call.call_count, 3)
        self.assertTrue(all("reply_markup" not in call.args[1] for call in api.call.call_args_list[:-1]))
        self.assertEqual(api.call.call_args.args[1]["reply_markup"], markup)


class ViolationCommandTests(TemporaryProject):
    def callback(self, data, user=77, chat=77, kind="private", bot_id=123456789):
        return {"update_id": 2, "callback_query": {"id": "fictional-callback", "data": data,
                "from": {"id": user, "is_bot": False},
                "message": {"message_id": 20, "from": {"id": bot_id}, "text": "Old table",
                            "chat": {"id": chat, "type": kind}}}}

    def handle(self, value):
        api, reporter = mock.Mock(), mock.Mock()
        bot.handle_update(self.config, SETTINGS, api, value, reporter=reporter, now=NOW)
        reporter.assert_not_called()
        return api

    def test_command_backfills_history_and_shows_counts_dates_and_delete_button(self):
        sportsite.write_json(self.root / "reports" / "example" / "report.json", report())
        api = self.handle(update(text="/violations"))
        self.assertIn("05.11.2026", api.send.call_args.args[1])
        self.assertIn("Поздних отмен: 1", api.send.call_args.args[1])
        markup = api.send.call_args.kwargs["reply_markup"]
        self.assertIn("Удалить", markup["inline_keyboard"][0][0]["text"])
        self.assertLessEqual(len(markup["inline_keyboard"][0][0]["callback_data"].encode()), 64)

    def test_owner_can_delete_in_bot_and_replayed_click_is_idempotent(self):
        registry = ViolationRegistry(self.config)
        registry.record(report())
        _, markup = bot.violations_page(self.config, registry.rows())
        value = self.callback(markup["inline_keyboard"][0][0]["callback_data"])
        api = self.handle(value)
        self.assertEqual(api.call.call_args_list[0].args[0], "answerCallbackQuery")
        self.assertEqual(api.call.call_args_list[-1].args[0], "editMessageText")
        self.assertIn("Список пуст", api.call.call_args.args[1]["text"])
        self.assertEqual(registry.rows(), [])
        self.handle(value)
        self.assertEqual(registry.rows(), [])

    def test_other_users_groups_spoofed_sender_and_malformed_callbacks_cannot_delete(self):
        registry = ViolationRegistry(self.config)
        registry.record(report())
        _, markup = bot.violations_page(self.config, registry.rows())
        data = markup["inline_keyboard"][0][0]["callback_data"]
        for value in (self.callback(data, user=88), self.callback(data, chat=88),
                      self.callback(data, kind="group"), self.callback(data, bot_id=999),
                      self.callback("vr:del:2:forged:0"), self.callback(None)):
            self.handle(value)
            self.assertEqual(len(registry.rows()), 1)
        api = self.handle(update(text="/violations", user=88))
        api.send.assert_not_called()

    def test_stale_button_refreshes_table_without_deleting_new_cases(self):
        registry = ViolationRegistry(self.config)
        registry.record(report())
        _, markup = bot.violations_page(self.config, registry.rows())
        registry.record(report(training_id=102))
        api = self.handle(self.callback(markup["inline_keyboard"][0][0]["callback_data"]))
        self.assertIn("изменился", api.call.call_args.args[1]["text"])
        self.assertEqual(registry.rows()[0]["count"], 2)

    def test_pagination_is_bounded_with_long_unicode_names_and_clamps_old_pages(self):
        rows = [{"student_id": ident, "full_name": "🏸" * 240, "count": 20,
                 "revision": "0" * 16,
                 "incidents": [{"training_start": report()["training_start"]}] * 20}
                for ident in range(1, 13)]
        text, markup = bot.violations_page(self.config, rows)
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 3500)
        self.assertEqual(len(markup["inline_keyboard"]), 6)
        self.assertIn("1/3", text)
        text, _ = bot.violations_page(self.config, rows, 999999)
        self.assertIn("3/3", text)

    def test_empty_register_and_corrupt_register_have_clear_responses(self):
        api = self.handle(update(text="/violations"))
        self.assertIn("Список пуст", api.send.call_args.args[1])
        path = ViolationRegistry(self.config).path
        sportsite.atomic_write(path, "bad json")
        api = self.handle(update(text="/violations"))
        self.assertIn("не перезаписаны", api.send.call_args.args[1])


class PairingTests(TemporaryProject):
    def test_pairing_needs_unpredictable_code_and_private_matching_identity(self):
        good = update(text="/start pairing-secret")
        self.assertTrue(bot.pairing_message(good, "pairing-secret"))
        for bad in (update(text="/start"), update(text="/start wrong"),
                    update(text="/start pairing-secret", kind="group"),
                    update(text="/start pairing-secret", user=88)):
            self.assertFalse(bot.pairing_message(bad, "pairing-secret"))

    def test_setup_stores_token_privately_binds_owner_and_never_prints_token(self):
        api = mock.Mock()
        api.call.side_effect = [{"id": 123456789, "is_bot": True, "username": SETTINGS["username"]},
                                {"url": ""}, True]
        api.updates.return_value = [update(1, "/start wrong", user=88, chat=88),
                                    update(2, "/start pairing-secret")]
        output = StringIO()
        with mock.patch.object(bot.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(bot.getpass, "getpass", return_value=TOKEN), \
                mock.patch.object(bot.secrets, "token_urlsafe", return_value="pairing-secret"), \
                mock.patch.object(bot, "TelegramAPI", return_value=api), \
                mock.patch.object(bot, "install_listener") as install, mock.patch("sys.stdout", output):
            bot.setup(self.config)
        saved = bot.load_settings(self.config)
        self.assertEqual(saved, SETTINGS)
        self.assertNotIn(TOKEN, output.getvalue())
        self.assertEqual(bot.settings_path(self.config).stat().st_mode & 0o777, 0o600)
        self.assertEqual(bot.settings_path(self.config).parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads((self.root / ".state/telegram-offset.json").read_text())["offset"], 3)
        install.assert_called_once()

    def test_existing_webhook_is_not_deleted_and_token_is_not_saved(self):
        api = mock.Mock()
        api.call.side_effect = [{"id": 123456789, "is_bot": True, "username": SETTINGS["username"]},
                                {"url": "https://example.invalid/other-service"}]
        with mock.patch.object(bot.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(bot.getpass, "getpass", return_value=TOKEN), \
                mock.patch.object(bot, "TelegramAPI", return_value=api), \
                mock.patch("builtins.print"), self.assertRaisesRegex(bot.TelegramError, "другой сервис"):
            bot.setup(self.config)
        self.assertFalse(bot.settings_path(self.config).exists())
        self.assertEqual([call.args[0] for call in api.call.call_args_list], ["getMe", "getWebhookInfo"])

    def test_invalid_or_group_settings_fail_closed(self):
        for key, value in (("chat_id", -10), ("user_id", 88), ("token", "invalid"), ("bot_id", True)):
            sportsite.write_json(bot.settings_path(self.config), dict(SETTINGS, **{key: value}))
            with self.assertRaises(bot.TelegramError):
                bot.load_settings(self.config)

    def test_only_one_listener_and_offset_survives_restart(self):
        with bot.listener_lock(self.config):
            with self.assertRaises(bot.TelegramError):
                with bot.listener_lock(self.config):
                    self.fail("Second listener started")
        sportsite.write_json(bot.settings_path(self.config), SETTINGS)
        sportsite.write_json(self.root / ".state/telegram-offset.json", {"bot_id": 123456789, "offset": 8})
        api = mock.Mock()
        api.updates.side_effect = [[update(7, "/help"), update(8, "/help")], KeyboardInterrupt]
        with mock.patch.object(bot, "TelegramAPI", return_value=api), self.assertRaises(KeyboardInterrupt):
            bot.serve(self.config)
        self.assertEqual(api.send.call_count, 1)
        self.assertEqual(api.updates.call_args.args[0], 9)
        self.assertEqual(json.loads((self.root / ".state/telegram-offset.json").read_text())["offset"], 9)


class WorkerTests(TemporaryProject):
    def test_error_payload_is_actionable_without_raw_worker_logs(self):
        process = mock.Mock()
        process.communicate.return_value = ('arbitrary log\n{"ok":false,"error":"Нет планового снимка"}\n', "")
        process.poll.return_value = 1
        process.returncode = 1
        with mock.patch.object(bot.subprocess, "Popen", return_value=process), \
                self.assertRaisesRegex(bot.TelegramError, "Нет планового снимка"):
            bot.run_report(self.config, date(2026, 11, 7))
        process.terminate.assert_not_called()

    def test_timeout_terminates_gracefully_to_restore_vpn(self):
        process = mock.Mock()
        process.communicate.side_effect = [subprocess.TimeoutExpired("worker", 240), ("", "")]
        process.poll.return_value = None
        with mock.patch.object(bot.subprocess, "Popen", return_value=process), self.assertRaises(bot.TelegramError):
            bot.run_report(self.config)
        process.terminate.assert_called_once()
        process.kill.assert_not_called()

    def test_listener_shutdown_also_terminates_report_worker(self):
        process = mock.Mock()
        process.communicate.side_effect = [KeyboardInterrupt, ("", "")]
        process.poll.return_value = None
        with mock.patch.object(bot.subprocess, "Popen", return_value=process), self.assertRaises(KeyboardInterrupt):
            bot.run_report(self.config)
        process.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
