"""Bounded trainer attendance edits; this module has no booking or cancellation API."""

import argparse
from contextlib import contextmanager
from datetime import date, timedelta
import fcntl
import json
import math
from pathlib import Path
import re
import secrets
import signal
import sys

from attendance_report import build_report, save_report
from sportsite import (Config, SportError, DataError, PermissionError, AttendanceWriteRejected,
                       AttendanceWriteUncertain, connect, parse_datetime, positive_id, private_dir, write_json)


class AttendanceError(SportError):
    pass


def eligible_starts(config):
    now = config.now()
    return sorted((s for offset in range(4) for s in config.starts(now.date()-timedelta(days=offset))
                   if s <= now <= s+timedelta(days=3)), reverse=True)


def editable_start(config, day):
    if isinstance(day, str):
        try:
            day = date.fromisoformat(day)
        except ValueError:
            raise AttendanceError("Укажи дату в формате YYYY-MM-DD.") from None
    starts = config.starts(day)
    if len(starts) != 1 or starts[0] not in eligible_starts(config):
        raise AttendanceError("Часы доступны от начала занятия до 72 часов после него.")
    return starts[0]


class ActionStore:
    def __init__(self, config):
        self.config = config
        self.path = config.root/".state/attendance-actions.json"

    @contextmanager
    def locked(self):
        private_dir(self.path.parent)
        with (self.path.parent/"attendance-actions.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = json.loads(self.path.read_text()) if self.path.exists() else {"schema_version": 1, "actions": {}, "pending": {}}
                if state["schema_version"] != 1 or not isinstance(state["actions"], dict) or not isinstance(state["pending"], dict):
                    raise ValueError()
                for key, action in state["actions"].items():
                    if not re.fullmatch(r"[0-9a-f]{16}", key) or action["op"] not in ("roster", "person", "prompt", "set"):
                        raise ValueError()
                    positive_id(action["owner"])
                    date.fromisoformat(action["date"])
                    parse_datetime(action["created_at"])
                    parse_datetime(action["expires_at"])
                    if action["status"] not in ("new", "sending", "uncertain", "done", "rejected"):
                        raise ValueError()
                    if action["op"] in ("person", "set"):
                        positive_id(action["person"]["student_id"])
                        if not isinstance(action["person"]["full_name"],str) or not action["person"]["full_name"].strip():
                            raise ValueError()
                    if action["op"] == "set":
                        positive_id(action["training_id"])
                        positive_id(action["group_id"])
                        if type(action["hours"]) is not int or not 0 <= action["hours"] <= 24:
                            raise ValueError()
                        if action["expected"] is not None and (type(action["expected"]) not in (int,float) or not math.isfinite(action["expected"])):
                            raise ValueError()
                        if action["status"] == "done" and not isinstance(action["result"],dict):
                            raise ValueError()
                for key,item in state["pending"].items():
                    positive_id(int(key))
                    date.fromisoformat(item["date"])
                    parse_datetime(item["expires_at"])
            except (OSError, ValueError, KeyError, TypeError, AttributeError, SportError):
                raise AttendanceError("Журнал выставления часов повреждён. Изменения не выполнялись.") from None
            yield state

    def save(self, state):
        write_json(self.path, state)

    def issue(self, owner, op, **data):
        positive_id(owner)
        with self.locked() as state:
            token = secrets.token_hex(8)
            state["actions"][token] = dict(data, owner=owner, op=op, status="new",
                created_at=self.config.now().isoformat(), expires_at=(self.config.now()+timedelta(minutes=15)).isoformat())
            # Missing old tokens always fail closed. Keep recent completed edits for replay/audit.
            if len(state["actions"]) > 2000:
                keys = sorted(state["actions"], key=lambda k: state["actions"][k]["created_at"])
                for key in keys[:len(keys)-2000]:
                    if state["actions"][key].get("status") not in ("sending", "uncertain"):
                        state["actions"].pop(key)
            self.save(state)
            return token

    def action(self, state, token, owner):
        action = state["actions"].get(token)
        if action is None or action["owner"] != owner:
            raise AttendanceError("Кнопка недоступна. Открой /attendance заново.")
        if action.get("status") not in ("done", "sending", "uncertain") and parse_datetime(action["expires_at"]) < self.config.now():
            raise AttendanceError("Кнопка устарела. Открой /attendance заново.")
        return action

    def lookup(self, token, owner):
        with self.locked() as state:
            return dict(self.action(state, token, owner))

    def pending(self, owner, day=None, clear=False):
        with self.locked() as state:
            key = str(owner)
            if clear:
                state["pending"].pop(key, None)
            elif day:
                editable_start(self.config, day)
                state["pending"][key] = {"date": day, "expires_at": (self.config.now()+timedelta(minutes=15)).isoformat()}
            else:
                item = state["pending"].get(key)
                return item["date"] if item and parse_datetime(item["expires_at"]) >= self.config.now() else None
            self.save(state)


class AttendanceService:
    def __init__(self, config, connector=connect):
        self.config, self.connector = config, connector
        self.store = ActionStore(config)

    def card(self, api, day, recovery=False):
        if recovery:
            starts = self.config.starts(date.fromisoformat(day))
            if len(starts) != 1:
                raise AttendanceError("Кнопка недоступна. Открой /attendance заново.")
            start = starts[0]
        else:
            start = editable_start(self.config, day)
        event = api.find(start)
        props = event["extendedProps"]
        if props.get("can_grade") is not True or (not recovery and props.get("can_edit") is not True):
            raise PermissionError("Нет права выставлять часы этому занятию.")
        detail = api.get("/api/training/%s" % props["id"])
        raw = api.get("/api/attendance/%s/grades" % props["id"])
        try:
            training = detail["training"]
            end = parse_datetime(training["end"])
            maximum = raw["academic_duration"]
            if (training["id"] != props["id"] or training["group"]["id"] != props["group_id"]
                    or raw["group_id"] != props["group_id"] or parse_datetime(training["start"]) != start
                    or parse_datetime(raw["start"]) != start or end <= start
                    or type(maximum) not in (int, float) or not math.isfinite(maximum)
                    or int(maximum) != maximum or not 1 <= maximum <= 24 or not isinstance(raw["grades"], list)):
                raise ValueError()
            people, seen = [], set()
            for row in raw["grades"]:
                ident = positive_id(row["student_id"])
                hours, name = row["hours"], row["full_name"]
                if (ident in seen or not isinstance(name, str) or not name.strip() or type(hours) not in (int,float)
                        or not math.isfinite(hours) or not 0 <= hours < 1000):
                    raise ValueError()
                seen.add(ident)
                people.append({"student_id": ident, "full_name": name.strip(), "hours": hours})
            people.sort(key=lambda p: (p["full_name"].casefold(),p["student_id"]))
            return {"date": start.date().isoformat(), "start": start.isoformat(), "end": end.isoformat(),
                    "training_id": props["id"], "group_id": props["group_id"], "max_hours": int(maximum), "people": people}
        except (KeyError, TypeError, ValueError, SportError):
            raise DataError("Не удалось проверить занятие и список перед выставлением часов.") from None

    def suggestions(self, api, card, query):
        query = query.strip()
        if not 3 <= len(query) <= 100 or not query.isprintable():
            raise AttendanceError("Для поиска введи от 3 до 100 символов имени или email.")
        raw = api.get("/api/attendance/suggest_student", {"term": query, "group_id": card["group_id"]})
        try:
            if not isinstance(raw, list) or len(raw) > 5:
                raise ValueError()
            people, seen = [], set()
            for row in raw:
                ident = positive_id(int(row["value"].split("_",1)[0]))
                match = re.fullmatch(r"(.+) \(([^()\s@]+@[^()\s@]+)\)", row["label"])
                if ident in seen or not match:
                    raise ValueError()
                seen.add(ident)
                people.append({"student_id": ident, "full_name": match[1].strip(), "email": match[2]})
            return people
        except (TypeError, ValueError, KeyError, AttributeError, SportError):
            raise DataError("Формат поиска студентов изменился. Часы не выставлены.") from None

    def person(self, api, card, target):
        ident = positive_id(target["student_id"])
        current = next((p for p in card["people"] if p["student_id"] == ident), None)
        if current:
            return current
        email = target.get("email")
        if email:
            matches = self.suggestions(api, card, email)
            person = next((p for p in matches if p["student_id"] == ident and p["email"].casefold() == email.casefold()), None)
            if person:
                return dict(person, hours=None)
        raise AttendanceError("Участника уже нет в таблице. Найди его по email заново.")

    def read(self, operation, day, query=None, person=None):
        editable_start(self.config, day)
        with self.connector(self.config) as api:
            card = self.card(api, day)
            if operation == "search":
                return {"card": card, "matches": self.suggestions(api, card, query)}
            if operation == "person":
                return {"card": card, "person": self.person(api, card, person)}
            return {"card": card}

    def reconcile(self, api, card, actor):
        if self.config.now() < parse_datetime(card["end"]):
            return "after_training"
        @contextmanager
        def reuse(_):
            yield api
        try:
            result = build_report(self.config, date.fromisoformat(card["date"]), connector=reuse)
            save_report(self.config, result, actor=actor)
            return "updated"
        except (SportError, OSError):
            # The website mark is already committed. Never misreport this as a failed grade.
            return "pending"

    def write(self, token, owner):
        with self.store.locked() as state:
            action = self.store.action(state, token, owner)
            if action["op"] != "set":
                raise AttendanceError("Кнопка недоступна. Открой /attendance заново.")
            if action["status"] == "done":
                return dict(action["result"], replay=True)
            recovering = action["status"] in ("sending", "uncertain")
            if not recovering:
                editable_start(self.config, action["date"])
            with self.connector(self.config) as api:
                card = self.card(api, action["date"], recovery=recovering)
                if card["training_id"] != action["training_id"] or card["group_id"] != action["group_id"]:
                    raise AttendanceError("Занятие изменилось. Открой /attendance заново.")
                hours = action["hours"]
                if type(hours) is not int or not 0 <= hours <= card["max_hours"]:
                    raise AttendanceError("Недопустимое количество часов.")
                current = next((p for p in card["people"] if p["student_id"] == action["person"]["student_id"]), None)
                actual = current["hours"] if current else None
                if recovering:
                    if (actual or 0) != hours:
                        action["status"] = "uncertain"
                        self.store.save(state)
                        raise AttendanceError("Результат прошлого сохранения не подтверждён. Обнови /attendance и проверь часы; повторной отправки не было.")
                else:
                    selected = self.person(api, card, action["person"])
                    if actual != action["expected"]:
                        raise AttendanceError("Часы уже изменились. Открой участника заново, чтобы не перезаписать чужое изменение.")
                    if (actual or 0) != hours:
                        action["status"] = "sending"
                        self.store.save(state)
                        try:
                            api.mark_attendance(parse_datetime(card["start"]), card["training_id"], selected["student_id"], hours)
                        except AttendanceWriteRejected:
                            action["status"] = "rejected"
                            self.store.save(state)
                            raise
                        except AttendanceWriteUncertain:
                            pass  # Verify, never blindly retry the POST.
                        try:
                            after = self.card(api, action["date"], recovery=True)
                            saved = next((p for p in after["people"] if p["student_id"] == selected["student_id"]), None)
                            if (saved["hours"] if saved else 0) != hours:
                                raise AttendanceError("Сохранение не подтверждено. Проверь часы на сайте и обнови /attendance.")
                        except SportError:
                            action["status"] = "uncertain"
                            self.store.save(state)
                            raise AttendanceError("Сохранение не подтверждено. Проверь часы на сайте и обнови /attendance.") from None
                result = {"date": card["date"], "person": action["person"], "hours": hours,
                          "reconciliation": self.reconcile(api, card, "telegram:%s" % owner), "replay": False}
                action.update(status="done", result=result, completed_at=self.config.now().isoformat())
                self.store.save(state)
                return result


def main():
    def stop_requested(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_requested)
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = Config.load(args.config)
        request = json.loads(sys.stdin.read(16_385))
        service = AttendanceService(config)
        if request["op"] == "set":
            result = service.write(request["token"], positive_id(request["owner"]))
        elif request["op"] in ("roster", "person", "search"):
            result = service.read(request["op"], request["date"], request.get("query"), request.get("person"))
        else:
            raise AttendanceError("Кнопка недоступна. Открой /attendance заново.")
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
        return 0
    except SportError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    except (KeyboardInterrupt, EOFError):
        return 130
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"ok": False, "error": "Не удалось обработать выставление часов. Часы проверь на сайте."}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
