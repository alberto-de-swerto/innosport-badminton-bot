"""Private Telegram attendance UI; all site access runs in an isolated worker."""

from datetime import date
import json
import re
import subprocess
import sys

from attendance_editor import ActionStore, AttendanceError, editable_start, eligible_starts
from attendance_report import display_name
from i18n import error_text, language_of, tr
from sportsite import ROOT, SportError, parse_datetime


def run_worker(config, payload):
    process = None
    try:
        process = subprocess.Popen([sys.executable, str(ROOT/"attendance_editor.py"), "--config", str(config.root/"config.json")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        output, _ = process.communicate(json.dumps(payload), timeout=150)
        result = json.loads(output.strip().splitlines()[-1])
        if result.get("ok") is False:
            raise AttendanceError(str(result["error"]))
        if process.returncode or result.get("ok") is not True or not isinstance(result["result"], dict):
            raise ValueError()
        return result["result"]
    except subprocess.TimeoutExpired:
        raise AttendanceError("Запрос занял слишком много времени. Обнови /attendance и проверь часы; автоматического повтора сохранения не будет.") from None
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        raise AttendanceError("Не удалось обработать выставление часов. Часы проверь на сайте.") from None
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=55)  # Let the worker restore the VPN before exit.
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()


class AttendanceUI:
    def __init__(self, config, settings, api, worker=run_worker):
        self.config, self.settings, self.api, self.worker = config, settings, api, worker
        self.store = ActionStore(config)
        self.owner = settings["user_id"]
        self.language = language_of(settings)

    def t(self, text):
        return tr(text, self.language)

    def button(self, label, operation, **data):
        return {"text": label, "callback_data": "at:"+self.store.issue(self.owner, operation, **data)}

    def show(self, text, keyboard, message=None):
        markup = {"inline_keyboard": keyboard}
        if message is None:
            self.api.send(self.settings["chat_id"], text, reply_markup=markup)
        elif message.get("text") != text or message.get("reply_markup") != markup:
            self.api.call("editMessageText", {"chat_id": self.settings["chat_id"], "message_id": message["message_id"],
                "text": text, "reply_markup": markup})

    def header(self, card):
        start = parse_datetime(card["start"]).astimezone(self.config.timezone)
        return self.t("🏸 Часы · %s МСК") % start.strftime("%d.%m.%Y %H:%M")

    def command(self, parts):
        try:
            command = parts[0].split("@",1)[0]
            if command == "/cancel":
                self.store.pending(self.owner, clear=True)
                self.api.send(self.settings["chat_id"], self.t("Поиск отменён. Открой /attendance для работы с часами."))
                return
            if command == "/attendance":
                self.store.pending(self.owner, clear=True)
                if len(parts) == 2:
                    self.roster(parts[1])
                elif len(parts) != 1:
                    raise AttendanceError("Формат: /attendance или /attendance YYYY-MM-DD.")
                else:
                    starts = eligible_starts(self.config)
                    keyboard = [[self.button(start.strftime("%d.%m.%Y %H:%M"), "roster", date=start.date().isoformat(), page=0)] for start in starts]
                    self.show(self.t("Выбери занятие для выставления часов. Доступны начавшиеся занятия за последние 72 часа.")
                              if starts else self.t("Сейчас нет занятий, для которых можно выставить часы."), keyboard)
            elif command == "/find":
                if len(parts) < 2:
                    raise AttendanceError("Формат: /find часть_email [YYYY-MM-DD]. Например: /find al.muk.")
                if len(parts) > 2 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[-1]):
                    day, query = parts[-1], " ".join(parts[1:-1])
                else:
                    starts = eligible_starts(self.config)
                    if not starts:
                        raise AttendanceError("Сейчас нет занятий, для которых можно выставить часы.")
                    day, query = starts[0].date().isoformat(), " ".join(parts[1:])
                self.store.pending(self.owner, day=day)
                self.search(day, query)
        except (SportError, OSError) as exc:
            self.fail(exc)

    def fail(self, error):
        self.api.send(self.settings["chat_id"], "⚠️ "+error_text(error, self.language))

    def pending_reply(self, text):
        try:
            day = self.store.pending(self.owner)
            if not day:
                return False
            self.search(day, text)
        except (SportError, OSError) as exc:
            self.fail(exc)
        return True

    def roster(self, day, page=0, message=None):
        editable_start(self.config, day)
        result = self.worker(self.config, {"op": "roster", "date": day})
        card = result["card"]
        page = max(0,min(page,max(0,(len(card["people"])-1)//6)))
        lines = [self.header(card), self.t("Выбери участника. Затем выбери количество часов — оно сразу сохранится на спортсайте."), ""]
        keyboard = []
        for person in card["people"][page*6:(page+1)*6]:
            label = "%s · %s" % (display_name(person)[:36],self.t("%s ч") % format(person["hours"],"g"))
            lines.append(label)
            keyboard.append([self.button(label, "person", date=day, person=person)])
        if not card["people"]:
            lines.append(self.t("В таблице пока нет участников. Используй поиск по email."))
        navigation = []
        if page:
            navigation.append(self.button(self.t("← Назад"), "roster", date=day, page=page-1))
        if (page+1)*6 < len(card["people"]):
            navigation.append(self.button(self.t("Далее →"), "roster", date=day, page=page+1))
        if navigation:
            keyboard.append(navigation)
        keyboard.append([self.button(self.t("🔎 Найти по email"), "prompt", date=day)])
        self.show("\n".join(lines), keyboard, message)

    def person(self, day, target, message=None):
        result = self.worker(self.config, {"op":"person", "date":day, "person":target})
        card, person = result["card"], result["person"]
        lines = [self.header(card),display_name(person)]
        if person.get("email"):
            lines.append(person["email"])
        lines.extend([self.t("Сейчас: %s ч") % format(person["hours"] or 0,"g"),
                      self.t("Выбери количество часов. Кнопка сразу сохраняет выбранное значение на сайте.")])
        def hour_button(label, hours):
            return self.button(label, "set", date=day, person=person, expected=person["hours"], hours=hours,
                               training_id=card["training_id"], group_id=card["group_id"])
        keyboard = []
        current = person["hours"] or 0
        if int(current) == current and current+1 <= card["max_hours"]:
            keyboard.append([hour_button(self.t("➕ 1 час → %s ч") % int(current+1),int(current+1))])
        buttons = [hour_button(self.t("%s ч") % hours,hours) for hours in range(card["max_hours"]+1)]
        keyboard.extend(buttons[i:i+4] for i in range(0,len(buttons),4))
        keyboard.append([self.button(self.t("← К участникам"), "roster", date=day, page=0)])
        self.show("\n".join(lines),keyboard,message)

    def search(self, day, query):
        result = self.worker(self.config,{"op":"search", "date":day, "query":query})
        card = result["card"]
        lines = [self.header(card), self.t("Совпадения по имени или email:"), ""]
        keyboard = []
        for person in result["matches"]:
            lines.append("%s (%s)" % (display_name(person),person["email"]))
            keyboard.append([self.button(display_name(person)[:50], "person", date=day, person=person)])
        if not result["matches"]:
            lines.append(self.t("Совпадений нет. Уточни часть имени или email, либо отправь /cancel."))
        self.show("\n".join(lines),keyboard)

    def callback(self, token, message):
        try:
            action = self.store.lookup(token,self.owner)
            if action["op"] == "roster":
                self.roster(action["date"], action.get("page",0),message)
            elif action["op"] == "person":
                self.person(action["date"],action["person"],message)
            elif action["op"] == "prompt":
                self.store.pending(self.owner,day=action["date"])
                self.api.send(self.settings["chat_id"], self.t("Напиши часть имени или email для занятия %s. Например: al.muk. /cancel — выйти из поиска.") % action["date"])
            elif action["op"] == "set":
                result = self.worker(self.config,{"op":"set", "token":token, "owner":self.owner})
                lines = [self.t("✅ Часы подтверждены на сайте: %s — %s ч") % (display_name(result["person"]),result["hours"])]
                if result.get("replay"):
                    lines.append(self.t("Это нажатие уже обработано; повторного начисления не было."))
                statuses = {"updated":"Список нарушений пересчитан.", "after_training":"Итоговую сверку /check выполни после окончания занятия.",
                            "pending":"Часы сохранены, но сверка нарушений не завершилась. Повтори /check для этой даты."}
                lines.append(self.t(statuses[result["reconciliation"]]))
                self.api.send(self.settings["chat_id"],"\n".join(lines))
                try:
                    self.roster(result["date"],message=message)
                except SportError:
                    self.api.send(self.settings["chat_id"],self.t("Часы сохранены. Для обновления списка открой /attendance заново."))
        except (SportError, OSError) as exc:
            self.fail(exc)
