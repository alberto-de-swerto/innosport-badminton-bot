"""A private Telegram command listener; scheduling remains a separate process."""

from contextlib import contextmanager
from datetime import date
import fcntl
import getpass
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import plistlib
import re
import secrets
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from attendance_report import display_name, load_baseline, text_report
from sportsite import (Config, INSTALL_ROOT, ROOT, SportError, atomic_write,
                       parse_datetime, private_dir, write_json)
from violation_registry import ViolationRegistry
from bot_notifications import ServiceNotifications
from i18n import (HELP_RU, LANGUAGES, error_text, help_text, language_of,
                  localize_notice, tr)

LOG = logging.getLogger("sportsite")
BOT_LABEL = "university.innopolis.badminton-telegram"
TOKEN_PATTERN = re.compile(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,200}\Z")
BOT_COMMANDS = [
    {"command": "check", "description": "Сравнить снимок записи и посещаемость"},
    {"command": "violations", "description": "Поздние отмены и удаление из журнала"},
    {"command": "undo", "description": "Отменить последнее списание или удаление"},
    {"command": "status", "description": "Снимки и ближайшая выгрузка"},
    {"command": "language", "description": "Язык сообщений"},
    {"command": "help", "description": "Как пользоваться ботом"},
]
HELP = HELP_RU


def bot_commands(language="ru"):
    return [dict(item, description=tr(item["description"], language)) for item in BOT_COMMANDS]


class TelegramError(SportError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TelegramAPI:
    def __init__(self, token, opener=None):
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            raise TelegramError("Неверный формат токена Telegram. Скопируй токен из @BotFather.")
        self._token = token
        self._opener = opener or build_opener(NoRedirect())

    def call(self, method, data=None, timeout=20):
        # Never print the URL, raw response or original exception: the URL contains the token.
        request = Request("https://api.telegram.org/bot" + self._token + "/" + method,
                          data=json.dumps(data or {}).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise TelegramError("Telegram прислал слишком большой ответ.")
                body = json.loads(raw)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            messages = {
                401: "Telegram отклонил токен. Повтори telegram-setup с действующим токеном.",
                403: "Telegram не разрешил отправку. Проверь, что бот не заблокирован.",
                409: "Этот бот уже принимает команды в другой программе либо использует webhook. Останови другой обработчик.",
                429: "Telegram временно ограничил запросы; повторим позже.",
            }
            raise TelegramError(messages.get(code, "Telegram вернул HTTP %s." % code)) from None
        except (URLError, OSError, ValueError):
            raise TelegramError("Нет ответа Telegram. Проверь интернет и доступ к Telegram через VPN.") from None
        if not isinstance(body, dict) or body.get("ok") is not True or "result" not in body:
            raise TelegramError("Telegram не подтвердил запрос.")
        return body["result"]

    def updates(self, offset, timeout=25):
        result = self.call("getUpdates", {"offset": offset, "timeout": timeout,
                                         "limit": 50, "allowed_updates": ["message", "callback_query"]}, timeout=timeout + 10)
        if (not isinstance(result, list) or any(not isinstance(row, dict)
                or type(row.get("update_id")) is not int or row["update_id"] < 0 for row in result)):
            raise TelegramError("Изменился формат команд Telegram.")
        return sorted(result, key=lambda row: row["update_id"])

    def send(self, chat_id, text, reply_markup=None):
        chunks = message_chunks(text)
        for index, chunk in enumerate(chunks):
            data = {"chat_id": chat_id, "text": chunk,
                    "link_preview_options": {"is_disabled": True}}
            if reply_markup is not None and index == len(chunks) - 1:
                data["reply_markup"] = reply_markup
            self.call("sendMessage", data)


def message_chunks(text, limit=3500):
    """Plain text avoids markup injection; account for UTF-16 length, including emoji."""
    chunks, current, count = [], "", 0
    for char in text:
        size = len(char.encode("utf-16-le")) // 2
        if count + size > limit:
            split = current.rfind("\n")
            if split > len(current) // 2:
                chunks.append(current[:split])
                current = current[split + 1:]
                count = len(current.encode("utf-16-le")) // 2
            else:
                chunks.append(current)
                current, count = "", 0
        current += char
        count += size
    if current:
        chunks.append(current)
    return chunks


def settings_path(config):
    return config.root / ".state" / "telegram.json"


def load_settings(config):
    try:
        raw = json.loads(settings_path(config).read_text(encoding="utf-8"))
        if (raw["schema_version"] != 1 or not TOKEN_PATTERN.fullmatch(raw["token"])
                or any(type(raw[key]) is not int or raw[key] <= 0 for key in ("chat_id", "user_id", "bot_id"))
                or raw["chat_id"] != raw["user_id"]
                or raw.get("language", "ru") not in LANGUAGES
                or not re.fullmatch(r"[A-Za-z0-9_]{5,64}", raw["username"])):
            raise ValueError()
        return raw
    except FileNotFoundError:
        raise TelegramError("Бот пока не подключён. Открой telegram-setup.command.") from None
    except (OSError, ValueError, KeyError, TypeError):
        raise TelegramError("Локальные настройки Telegram повреждены. Повтори telegram-setup.") from None


def snapshot_notification(snapshot, config, language="ru"):
    _ = lambda text: tr(text, language)
    start = parse_datetime(snapshot["training_start"]).astimezone(config.timezone)
    captured = parse_datetime(snapshot["captured_at"]).astimezone(config.timezone)
    lines = [_("✅ Список за час до тренировки успешно сохранён"),
             _("🏸 Beginners · %s МСК") % start.strftime("%d.%m.%Y %H:%M"),
             _("Выгрузка: %s МСК") % captured.strftime("%d.%m %H:%M:%S"),
             _("Планируют прийти: %s") % snapshot["participant_count"], ""]
    lines.extend("%s. %s" % (index, display_name(person))
                 for index, person in enumerate(snapshot["participants"], 1))
    if not snapshot["participants"]:
        lines.append(_("Пока никто не записан."))
    lines.extend(["", _("После занятия сохрани часы на сайте и отправь /check.")])
    return "\n".join(lines)


class SnapshotNotifications:
    """Deliver only completed scheduled exports, without delaying the snapshot worker."""
    def __init__(self, config, settings, api):
        self.config, self.settings, self.api = config, settings, api
        self.state_path = config.root / ".state" / "telegram-notifications.json"

    def load_state(self):
        if not self.state_path.exists():
            state = {"schema_version": 1, "chat_id": self.settings["chat_id"],
                     "enabled_at": self.config.now().isoformat(), "deliveries": {}}
            write_json(self.state_path, state)
            return state
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if (state["schema_version"] != 1 or state["chat_id"] != self.settings["chat_id"]
                    or not isinstance(state["deliveries"], dict)):
                raise ValueError()
            parse_datetime(state["enabled_at"])
            for entry in state["deliveries"].values():
                if (type(entry["parts_sent"]) is not int or type(entry["part_count"]) is not int
                        or not 0 <= entry["parts_sent"] <= entry["part_count"] or entry["part_count"] < 1
                        or entry.get("language", "ru") not in LANGUAGES
                        or not re.fullmatch(r"[0-9a-f]{64}", entry["snapshot_sha256"])):
                    raise ValueError()
            return state
        except (OSError, ValueError, TypeError, KeyError, AttributeError, SportError):
            raise TelegramError("Повреждён журнал уведомлений Telegram. Плановые снимки продолжают сохраняться.") from None

    def tick(self):
        private_dir(self.state_path.parent)
        with (self.state_path.parent / "telegram-notifications.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            # The lock is released when the file closes, including on exceptions.
            state = self.load_state()
            enabled_at = parse_datetime(state["enabled_at"])
            sent = 0
            for path in sorted((self.config.root / "exports").glob("*_training-*/snapshot.json")):
                if "_manual_" in path.parent.name:
                    continue
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if raw.get("mode") != "scheduled":
                        continue
                    captured = parse_datetime(raw["captured_at"])
                    if captured < enabled_at:
                        continue
                    start = parse_datetime(raw["training_start"]).astimezone(self.config.timezone)
                    if captured > self.config.now() or start not in self.config.starts(start.date()):
                        raise ValueError()
                    snapshot = load_baseline(self.config, start)
                    key = self.config.job_key(start) + "_training-" + str(snapshot["training_id"])
                    if snapshot["mode"] != "scheduled" or key != path.parent.name:
                        raise ValueError()
                except (OSError, ValueError, TypeError, KeyError, AttributeError, SportError):
                    LOG.warning("Telegram: уведомление пропущено — плановый снимок не прошёл проверку.")
                    continue
                entry = state["deliveries"].get(key)
                # Freeze a delivery's language so partially sent old notifications
                # retain their chunk boundaries when the owner changes language.
                language = entry.get("language", "ru") if entry is not None else language_of(self.settings)
                chunks = message_chunks(snapshot_notification(snapshot, self.config, language))
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if entry is None:
                    entry = {"snapshot_sha256": digest, "parts_sent": 0, "part_count": len(chunks), "language": language}
                    state["deliveries"][key] = entry
                    write_json(self.state_path, state)
                if entry["snapshot_sha256"] != digest or entry["part_count"] != len(chunks):
                    LOG.error("Telegram: ранее сохранённый снимок изменён; повторная отправка остановлена.")
                    continue
                if entry["parts_sent"] == len(chunks):
                    continue
                for index in range(entry["parts_sent"], len(chunks)):
                    self.api.send(self.settings["chat_id"], chunks[index])
                    entry["parts_sent"] = index + 1
                    if entry["parts_sent"] == len(chunks):
                        entry["sent_at"] = self.config.now().isoformat()
                    write_json(self.state_path, state)
                sent += 1
                LOG.info("Telegram: уведомление об успешной плановой выгрузке отправлено (%s).", key)
            return sent


@contextmanager
def listener_lock(config):
    state_dir = config.root / ".state"
    private_dir(state_dir)
    with (state_dir / "telegram.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TelegramError("Обработчик этого бота уже запущен. Второй экземпляр не нужен.") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def pairing_message(update, code):
    message = update.get("message", {})
    chat, sender = message.get("chat", {}), message.get("from", {})
    text = message.get("text", "")
    return (chat.get("type") == "private" and type(chat.get("id")) is int and chat["id"] > 0
            and sender.get("id") == chat["id"] and not sender.get("is_bot", True)
            and isinstance(text, str) and hmac.compare_digest(text.encode(), ("/start " + code).encode()))


def setup(config):
    if settings_path(config).exists():
        # Existing pairing is never silently reassigned to another person.
        load_settings(config)
        install_listener(config)
        print("Бот уже подключён к личному чату. Автозапуск проверен. Отправь /check своему боту.")
        return
    if not sys.stdin.isatty():
        raise TelegramError("Открой telegram-setup.command в Терминале для скрытого ввода токена.")
    print("Токен вводится только здесь, не отображается и не попадает в историю команд.")
    token = getpass.getpass("Вставь токен из @BotFather и нажми Enter: ").strip()
    api = TelegramAPI(token)
    me = api.call("getMe")
    if (not isinstance(me, dict) or me.get("is_bot") is not True
            or type(me.get("id")) is not int or me["id"] <= 0
            or not re.fullmatch(r"[A-Za-z0-9_]{5,64}", me.get("username", ""))):
        raise TelegramError("Не удалось проверить имя бота.")
    webhook = api.call("getWebhookInfo")
    if not isinstance(webhook, dict) or "url" not in webhook:
        raise TelegramError("Не удалось проверить настройки бота в Telegram.")
    if webhook["url"]:
        raise TelegramError("У бота уже подключён другой сервис (webhook). Используй отдельного бота или отключи тот сервис.")
    code = secrets.token_urlsafe(24)
    print("\nОткрой ссылку в своём Telegram и нажми Start / Запустить:\n"
          "https://t.me/%s?start=%s\n\nОжидаю подтверждение в течение 10 минут…" % (me["username"], code))
    with listener_lock(config):
        offset, deadline, paired = 0, time.monotonic() + 600, None
        while time.monotonic() < deadline and paired is None:
            for update in api.updates(offset):
                offset = update["update_id"] + 1
                if pairing_message(update, code):
                    paired = update["message"]
                    break
        if paired is None:
            raise TelegramError("Подтверждение не получено. Запусти подключение заново.")
        settings = {"schema_version": 1, "token": token, "bot_id": me["id"], "username": me["username"],
                    "chat_id": paired["chat"]["id"], "user_id": paired["from"]["id"]}
        write_json(settings_path(config), settings)
        write_json(config.root / ".state" / "telegram-offset.json", {"bot_id": me["id"], "offset": offset})
        api.call("setMyCommands", {"commands": BOT_COMMANDS})
        api.send(settings["chat_id"], "✅ Бот подключён к твоему личному чату.\n\n" + HELP)
    install_listener(config)
    print("Готово. Бот работает в фоне и запускается при входе в macOS. Mac должен быть включён и не спать.")


def run_report(config, requested_date=None, language="ru"):
    args = [sys.executable, str(ROOT / "sportsite.py"), "--config", str(config.root / "config.json"),
            "report", "--json"]
    if requested_date:
        args.extend(["--date", requested_date.isoformat()])
    process = None
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        output, _ = process.communicate(timeout=240)
    except subprocess.TimeoutExpired:
        raise TelegramError("Проверка заняла слишком много времени. Повтори /check через минуту.") from None
    except OSError:
        raise TelegramError("Не удалось запустить сравнение. Проверь установку программы.") from None
    finally:
        if process is not None and process.poll() is None:
            # SIGTERM executes the worker's finally blocks and restores the VPN.
            process.terminate()
            try:
                process.communicate(timeout=55)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
    try:
        payload = json.loads(output.strip().splitlines()[-1])
        if payload.get("ok") is False:
            raise TelegramError(str(payload["error"]))
        if process.returncode or payload.get("ok") is not True or not isinstance(payload["report"], dict):
            raise ValueError()
        return text_report(payload["report"], language) + "\n\n" + tr("📒 Журнал поздних отмен и удаление записей: /violations", language)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise TelegramError("Сравнение не завершилось. Проверь сессию спортсайта через login.command и повтори /check.") from None


def status_text(config, language="ru"):
    _ = lambda text: tr(text, language)
    now = config.now()
    due = [config.due_at(start) for start in config.upcoming(now, 8) if config.due_at(start) > now]
    lines = [_("🏸 Beginners · состояние"), _("Бот принимает команды.")]
    if due:
        lines.append(_("Ближайшая выгрузка: %s МСК.") % min(due).strftime("%d.%m.%Y %H:%M"))
    try:
        jobs_path = config.root / ".state" / "jobs.json"
        jobs = json.loads(jobs_path.read_text(encoding="utf-8")) if jobs_path.exists() else {}
        for key in sorted(jobs, reverse=True)[:3]:
            status = jobs[key].get("status", "неизвестно")
            status = {"saved": "✅ снимок сохранён", "missed": "❌ снимок пропущен", "retry": "⏳ повторяем выгрузку"}.get(status, status)
            lines.append("%s: %s" % (key.replace("_", " "), _(status)))
    except (OSError, ValueError, TypeError, AttributeError):
        lines.append(_("⚠️ Не удалось прочитать журнал снимков."))
    lines.append(_("Mac должен быть включён и не спать. Сессию сайта эта команда не проверяет."))
    return "\n".join(lines)


def violations_page(config, rows, page=0, notice=None, undo_id=None, language="ru"):
    _ = lambda text: tr(text, language)
    page_size = 5
    pages = max(1, (len(rows) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    lines = [_("📒 Beginners · поздние отмены"), ""]
    if notice:
        lines.extend([localize_notice(notice, language), ""])
    lines.extend([_("Участников: %s · случаев: %s") % (len(rows), sum(p["count"] for p in rows)),
                  _("Страница %s/%s") % (page + 1, pages)])
    keyboard = []
    for index, person in enumerate(rows[page * page_size:(page + 1) * page_size], page * page_size + 1):
        name = display_name(person)[:120]
        dates = [parse_datetime(item["training_start"]).astimezone(config.timezone).strftime("%d.%m.%Y")
                 + ("*" if item.get("source") == "confirmed_import" else "")
                 for item in person["incidents"][:4]]
        suffix = _(" · ещё %s") % (person["count"] - 4) if person["count"] > 4 else ""
        lines.extend(["", "%s. %s" % (index, name),
                      _("Счётчик: %s · %s%s") % (person["count"], ", ".join(dates), suffix)])
        keyboard.append([{"text": "−1 · " + name[:32],
                          "callback_data": "vr:dec:%s:%s:%s" % (person["student_id"], person["revision"], page)},
                         {"text": _("🗑 Удалить все"),
                          "callback_data": "vr:del:%s:%s:%s" % (person["student_id"], person["revision"], page)}])
    if not rows:
        lines.extend(["", _("Список пуст. Записи добавляются после /check по завершённым занятиям.")])
    navigation = []
    if page:
        navigation.append({"text": _("← Назад"), "callback_data": "vr:page:%s" % (page - 1)})
    if page + 1 < pages:
        navigation.append({"text": _("Далее →"), "callback_data": "vr:page:%s" % (page + 1)})
    if navigation:
        keyboard.append(navigation)
    if undo_id:
        keyboard.append([{"text": _("↩️ Отменить последнее действие"), "callback_data": "vr:undo:" + undo_id}])
    lines.extend(["", _("Это возможные поздние отмены: были в снимке, затем исчезли без часов. "
                  "Кто отменил запись, сайт не сообщает."),
                  _("«−1» списывает самый ранний случай. При 0 участник исчезает из списка. "
                  "«🗑 Удалить все» обнуляет счётчик. Повторный /check не вернёт списанное; новые отмены учитываются."),
                  _("* — случай со скриншотов, подтверждённый вручную. "
                  "Остальные тесты и предварительные проверки не учитываются.")])
    return "\n".join(lines), {"inline_keyboard": keyboard}


def show_violations(config, settings, api, page=0, message=None, notice=None):
    registry = ViolationRegistry(config)
    registry.sync_history()
    text, markup = violations_page(config, registry.rows(), page, notice, undo_id=registry.latest_undo(), language=language_of(settings))
    if message is None:
        api.send(settings["chat_id"], text, reply_markup=markup)
    elif message.get("text") != text or message.get("reply_markup") != markup:
        api.call("editMessageText", {"chat_id": settings["chat_id"], "message_id": message["message_id"],
                                     "text": text, "reply_markup": markup})


def show_language(settings, api, notice=None):
    current = language_of(settings)
    keyboard = [[{"text": ("✅ " if code == current else "") + label,
                  "callback_data": "language:" + code}
                 for code, label in (("ru", "Русский"), ("en", "English"))]]
    text = "Выбери язык / Choose a language"
    if notice:
        text = notice + "\n\n" + text
    api.send(settings["chat_id"], text, reply_markup={"inline_keyboard": keyboard})


def change_language(config, settings, api, language):
    if language not in LANGUAGES:
        raise TelegramError("Формат: /language, /language ru или /language en.")
    updated = dict(settings, language=language)
    try:
        write_json(settings_path(config), updated)
    except OSError:
        raise TelegramError("Не удалось сохранить язык. Попробуй ещё раз.") from None
    # Workers share this dictionary; update it only after the preference is durable.
    settings.update(updated)
    try:
        api.call("setMyCommands", {"commands": bot_commands(language)})
    except TelegramError:
        LOG.warning("Telegram: язык сохранён, но меню команд не обновлено.")
    show_language(settings, api, tr("Язык переключён на русский.", language))


def handle_callback(config, settings, api, callback):
    message, sender = callback.get("message", {}), callback.get("from", {})
    chat = message.get("chat", {})
    if (chat.get("type") != "private" or chat.get("id") != settings["chat_id"]
            or sender.get("id") != settings["user_id"] or sender.get("is_bot", True)
            or message.get("from", {}).get("id") != settings["bot_id"]
            or type(message.get("message_id")) is not int or message["message_id"] <= 0
            or not isinstance(callback.get("id"), str)):
        return
    data = callback.get("data", "")
    pattern = (r"vr:(?:page:(?P<page>[0-9]{1,6})|undo:(?P<undo>[0-9a-f]{16})|"
               r"(?P<action>del|dec):(?P<student>[1-9][0-9]{0,18}):"
               r"(?P<revision>[0-9a-f]{16}):(?P<return_page>[0-9]{1,6}))")
    match = re.fullmatch(pattern, data) if isinstance(data, str) else None
    language_match = re.fullmatch(r"language:(ru|en)", data) if isinstance(data, str) else None
    try:
        api.call("answerCallbackQuery", {"callback_query_id": callback["id"]})
    except TelegramError:
        # An expired callback must not stall the update queue or perform a write.
        LOG.warning("Telegram: не удалось подтвердить нажатие кнопки; действие не выполнено.")
        return
    if language_match:
        try:
            change_language(config, settings, api, language_match[1])
        except (SportError, OSError) as exc:
            api.send(settings["chat_id"], "⚠️ " + error_text(exc, language_of(settings)))
        return
    if match is None:
        return
    try:
        notice = None
        if match["student"]:
            registry = ViolationRegistry(config)
            registry.sync_history()
            operation = registry.decrement if match["action"] == "dec" else registry.dismiss
            notice = operation(int(match["student"]), match["revision"])
        elif match["undo"]:
            registry = ViolationRegistry(config)
            registry.sync_history()
            notice = registry.undo(expected_id=match["undo"], request_id="callback:" + callback["id"])
        show_violations(config, settings, api, int(match["page"] or match["return_page"] or 0), message=message, notice=notice)
    except (SportError, OSError) as exc:
        detail = exc if isinstance(exc, SportError) else "Не удалось сохранить журнал поздних отмен."
        api.send(settings["chat_id"], "⚠️ " + error_text(detail, language_of(settings)))


def handle_update(config, settings, api, update, reporter=run_report, now=None):
    if "callback_query" in update:
        handle_callback(config, settings, api, update["callback_query"])
        return
    message = update.get("message", {})
    chat, sender = message.get("chat", {}), message.get("from", {})
    if (chat.get("type") != "private" or chat.get("id") != settings["chat_id"]
            or sender.get("id") != settings["user_id"] or sender.get("is_bot", True)):
        LOG.info("Telegram: обновление пропущено (личный чат: %s, чат владельца: %s, отправитель владелец: %s).",
                 chat.get("type") == "private", chat.get("id") == settings["chat_id"],
                 sender.get("id") == settings["user_id"])
        return
    language = language_of(settings)
    _ = lambda text: tr(text, language)
    text = message.get("text", "")
    if not isinstance(text, str) or not text.startswith("/"):
        LOG.info("Telegram: получено сообщение владельца без команды.")
        api.send(settings["chat_id"], _("Отправь /check для сравнения или /status для состояния выгрузок."))
        return
    if type(message.get("date")) is not int or (now if now is not None else time.time()) - message["date"] > 900:
        api.send(settings["chat_id"], _("Команда пришла более 15 минут назад. Отправь её заново, чтобы проверить нужное занятие."))
        return
    parts = text.split()
    command, separator, mention = parts[0].partition("@")
    if mention and mention.casefold() != settings["username"].casefold():
        return
    if command in ("/start", "/help"):
        api.send(settings["chat_id"], help_text(language))
        LOG.info("Telegram: справка отправлена.")
    elif command == "/status":
        api.send(settings["chat_id"], status_text(config, language))
        LOG.info("Telegram: ответ /status отправлен.")
    elif command == "/language":
        if len(parts) == 1:
            show_language(settings, api)
        elif len(parts) == 2 and parts[1].lower() in LANGUAGES:
            try:
                change_language(config, settings, api, parts[1].lower())
            except (SportError, OSError) as exc:
                api.send(settings["chat_id"], "⚠️ " + error_text(exc, language_of(settings)))
        else:
            api.send(settings["chat_id"], _("Формат: /language, /language ru или /language en."))
    elif command == "/violations":
        try:
            if len(parts) != 1:
                api.send(settings["chat_id"], _("Отправь /violations без параметров. Страницы переключаются кнопками."))
                return
            show_violations(config, settings, api)
        except (SportError, OSError) as exc:
            detail = exc if isinstance(exc, SportError) else "Не удалось открыть журнал поздних отмен."
            api.send(settings["chat_id"], "⚠️ " + error_text(detail, language))
    elif command == "/undo":
        if len(parts) != 1:
            api.send(settings["chat_id"], _("Отправь /undo без параметров — отменить последнее списание или удаление."))
            return
        try:
            registry = ViolationRegistry(config)
            registry.sync_history()
            answer = registry.undo(request_id="update:" + str(update["update_id"]))
            show_violations(config, settings, api, notice=answer)
        except (SportError, OSError) as exc:
            detail = exc if isinstance(exc, SportError) else "Не удалось сохранить отмену действия."
            api.send(settings["chat_id"], "⚠️ " + error_text(detail, language))
    elif command == "/check":
        try:
            if len(parts) > 2 or (len(parts) == 2 and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[1])):
                raise ValueError()
            requested_date = date.fromisoformat(parts[1]) if len(parts) == 2 else None
        except ValueError:
            api.send(settings["chat_id"], _("Формат: /check или /check YYYY-MM-DD, например /check 2026-11-07."))
            return
        api.send(settings["chat_id"], _("🔎 Сравниваю исходный список с сохранёнными на сайте часами…"))
        try:
            answer = reporter(config, requested_date, language=language)
        except SportError as exc:
            answer = "⚠️ " + error_text(exc, language)
        api.send(settings["chat_id"], answer)
        LOG.info("Telegram: ответ /check отправлен.")
    else:
        api.send(settings["chat_id"], help_text(language))


def serve(config):
    with listener_lock(config):
        settings = load_settings(config)
        api = TelegramAPI(settings["token"])
        offset_path = config.root / ".state" / "telegram-offset.json"
        try:
            saved = json.loads(offset_path.read_text(encoding="utf-8"))
            if saved["bot_id"] != settings["bot_id"] or type(saved["offset"]) is not int or saved["offset"] < 0:
                raise ValueError()
            offset = saved["offset"]
        except FileNotFoundError:
            offset = 0
        except (OSError, ValueError, KeyError, TypeError):
            raise TelegramError("Повреждён журнал команд Telegram. Нужна проверка telegram-offset.json.") from None
        LOG.info("Telegram-бот включён; доступ только у привязанного владельца в личном чате.")
        try:
            api.call("setMyCommands", {"commands": bot_commands(language_of(settings))})
        except TelegramError as exc:
            LOG.warning("Telegram: меню команд не обновлено: %s", exc)
        notifications = [SnapshotNotifications(config, settings, api), ServiceNotifications(config, settings, api)]
        notify_after = [0] * len(notifications)
        backoff = 2
        while True:
            for index, worker in enumerate(notifications):
                if time.monotonic() >= notify_after[index]:
                    try:
                        worker.tick()
                        notify_after[index] = 0
                    except (SportError, OSError) as exc:
                        detail = str(exc) if isinstance(exc, SportError) else "Не удалось сохранить журнал уведомления."
                        LOG.warning("Telegram, уведомления: %s", detail)
                        notify_after[index] = time.monotonic() + 60
            try:
                for update in api.updates(offset):
                    if update["update_id"] < offset:
                        continue
                    handle_update(config, settings, api, update)
                    offset = update["update_id"] + 1
                    write_json(offset_path, {"bot_id": settings["bot_id"], "offset": offset})
                backoff = 2
            except TelegramError as exc:
                LOG.warning("Telegram: %s", exc)
                time.sleep(backoff)
                backoff = min(60, backoff * 2)


def install_listener(config):
    if sys.platform != "darwin":
        raise TelegramError("Для постоянной работы на этой системе запусти sportsite.py telegram-run.")
    # Use the same protected runtime as the snapshot agent, with an independent listener.
    private_dir(INSTALL_ROOT)
    for name in ("sportsite.py", "attendance_report.py", "telegram_bot.py", "vpn_control.py", "api_auth.py",
                 "violation_registry.py", "bot_notifications.py", "i18n.py"):
        source, target = ROOT / name, INSTALL_ROOT / name
        if source.resolve() != target.resolve():
            atomic_write(target, source.read_text(encoding="utf-8"))
    if config.root.resolve() != INSTALL_ROOT.resolve():
        raise TelegramError("Сначала установи автозапуск основного скрипта через install-autostart, затем повтори подключение бота.")
    private_dir(config.root / "logs")
    paths = [config.root / "logs" / name for name in ("telegram.log", "telegram-errors.log")]
    for path in paths:
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
    definition = {
        "Label": BOT_LABEL,
        "ProgramArguments": [str(config.root / ".venv/bin/python"), str(config.root / "sportsite.py"),
                             "--config", str(config.root / "config.json"), "telegram-run"],
        "WorkingDirectory": str(config.root), "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "TZ": config.timezone.key},
        "StandardOutPath": str(paths[0]), "StandardErrorPath": str(paths[1]),
    }
    plist_path = Path.home() / "Library/LaunchAgents" / (BOT_LABEL + ".plist")
    target = "gui/%s/%s" % (os.getuid(), BOT_LABEL)
    existing = subprocess.run(["launchctl", "print", target], capture_output=True).returncode == 0
    if existing:
        result = subprocess.run(["launchctl", "kickstart", "-k", target], capture_output=True)
    else:
        atomic_write(plist_path, plistlib.dumps(definition).decode("utf-8"))
        result = subprocess.run(["launchctl", "bootstrap", "gui/%s" % os.getuid(), str(plist_path)], capture_output=True)
    if result.returncode:
        raise TelegramError("Не удалось включить фоновую работу бота. Повтори telegram-setup.")


def show_status(config):
    settings = load_settings(config)
    print("Бот: @%s. Доступ: только привязанный личный чат. Токен скрыт." % settings["username"])
    print(status_text(config))
