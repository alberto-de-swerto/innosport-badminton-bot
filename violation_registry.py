"""Private, deduplicated register of possible late cancellations."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import logging
import re
import secrets
import threading

from sportsite import DataError, parse_datetime, positive_id, private_dir, write_json


LOG = logging.getLogger("sportsite")


def session_key(session):
    return "%s:%s" % (session["group_id"], session["training_id"])


def incident_key(session, student_id):
    return "%s:%s" % (session_key(session), student_id)


def validate_session(session):
    positive_id(session["group_id"])
    positive_id(session["training_id"])
    if parse_datetime(session["checked_at"]) < parse_datetime(session["training_start"]):
        raise ValueError()
    if not isinstance(session["cancelled"], list):
        raise ValueError()
    ids = set()
    for person in session["cancelled"]:
        ident = positive_id(person["student_id"])
        if (ident in ids or not isinstance(person["full_name"], str)
                or not person["full_name"].strip()):
            raise ValueError()
        ids.add(ident)


def report_session(report, allow_import=False):
    """Imported reports require explicit approval of an individual case."""
    if (report.get("baseline_mode") == "user_import" and not allow_import) or report.get("preliminary") is True:
        return None
    try:
        modes = ("scheduled", "user_import") if allow_import else ("scheduled",)
        if (report["schema_version"] != 1 or report["baseline_mode"] not in modes
                or report["preliminary"] is not False
                or not parse_datetime(report["training_start"]) < parse_datetime(report["training_end"])
                <= parse_datetime(report["checked_at"])):
            raise ValueError()
        session = {key: report[key] for key in
                   ("training_id", "group_id", "training_start", "checked_at")}
        session["cancelled"] = [{"student_id": p["student_id"], "full_name": p["full_name"]}
                                for p in report["cancelled"]]
        validate_session(session)
        registered = {positive_id(p["student_id"]) for p in report["registered"]}
        if any(p["student_id"] not in registered for p in session["cancelled"]):
            raise ValueError()
        return session
    except (ValueError, KeyError, TypeError, AttributeError, DataError):
        raise DataError("Отчёт не прошёл проверку для журнала поздних отмен.") from None


class ViolationRegistry:
    def __init__(self, config, actor="system", request_id=None, reason=None):
        self.config = config
        self.path = config.root / ".state" / "violations.json"
        self.actor, self.request_id = actor, request_id
        self.reason = reason
        self._local = threading.local()
        self.database = None
        if config.registry_backend == "postgresql":
            from registry_database import RegistryDatabase
            self.database = RegistryDatabase(config)

    @contextmanager
    def locked(self):
        if self.database is not None:
            with self.database.transaction() as connection:
                self._local.connection = connection
                try:
                    yield
                finally:
                    self._local.connection = None
            return
        private_dir(self.path.parent)
        with (self.path.parent / "violations.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def load(self):
        if self.database is not None:
            connection = getattr(self._local, "connection", None)
            if connection is None:
                with self.locked():
                    return self.load()
            return self.database.load(connection)
        return self.load_json()

    def load_json(self):
        """Legacy input remains readable for explicit migration and recovery."""
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if (state["schema_version"] != 1 or not isinstance(state["sessions"], dict)
                    or not isinstance(state["dismissed"], list)
                    or any(not isinstance(key, str) or not re.fullmatch(r"[1-9]\d*:[1-9]\d*:[1-9]\d*", key)
                           for key in state["dismissed"])):
                raise ValueError()
            for key, session in state["sessions"].items():
                validate_session(session)
                if key != session_key(session):
                    raise ValueError()
            # Additive migration of existing registers; automatic imports remain disabled.
            state.setdefault("confirmed_cases", {})
            if not isinstance(state["confirmed_cases"], dict):
                raise ValueError()
            for key, case in state["confirmed_cases"].items():
                validate_session(dict(case, cancelled=[case["person"]]))
                parse_datetime(case["confirmed_at"])
                if (case["source"] != "confirmed_import"
                        or key != incident_key(case, case["person"]["student_id"])):
                    raise ValueError()
            state.setdefault("undo_history", [])
            state.setdefault("undo_results", {})
            if (not isinstance(state["undo_history"], list) or len(state["undo_history"]) > 20
                    or not isinstance(state["undo_results"], dict) or len(state["undo_results"]) > 50
                    or any(not isinstance(k, str) or not isinstance(v, str)
                           for k, v in state["undo_results"].items())):
                raise ValueError()
            for action in state["undo_history"]:
                if (not re.fullmatch(r"[0-9a-f]{16}", action["id"])
                        or action["kind"] not in ("decrement", "dismiss")
                        or not isinstance(action["cases"], list) or not action["cases"]):
                    raise ValueError()
                parse_datetime(action["created_at"])
                for case in action["cases"]:
                    validate_session(dict(case, cancelled=[case["person"]]))
                    if case["source"] not in ("scheduled", "confirmed_import"):
                        raise ValueError()
                    if case["source"] == "confirmed_import":
                        parse_datetime(case["confirmed_at"])
            return state
        except FileNotFoundError:
            return {"schema_version": 1, "sessions": {}, "dismissed": [], "confirmed_cases": {},
                    "undo_history": [], "undo_results": {}}
        except (OSError, ValueError, KeyError, TypeError, AttributeError, DataError):
            raise DataError("Не удалось прочитать журнал поздних отмен. Данные не перезаписаны.") from None

    def save(self, state, kind, reason, metadata=None, request_id=None):
        if self.database is None:
            write_json(self.path, state)
        else:
            self.database.save(self._local.connection, state, actor=self.actor, kind=kind,
                               reason=self.reason or reason, metadata=metadata or {},
                               request_id=request_id or self.request_id)

    def audit(self, limit=10):
        if self.database is None:
            return []
        return self.database.audit(limit)

    @staticmethod
    def merge(state, session):
        key = session_key(session)
        previous = state["sessions"].get(key)
        if previous and parse_datetime(previous["checked_at"]) >= parse_datetime(session["checked_at"]):
            return False
        # Retain current source data, including dismissed cases, so undo can respect
        # later attendance corrections without rolling the entire register back.
        state["sessions"][key] = session
        return True

    def record(self, report):
        session = report_session(report)
        if session is None:
            return
        with self.locked():
            state = self.load()
            if self.merge(state, session):
                self.save(state, "report", "Attendance comparison saved", {"session_keys": [session_key(session)]})

    def confirm_import(self, report, student_id):
        """Approve one known screenshot case without relabelling its source as automatic."""
        positive_id(student_id)
        session = report_session(report, allow_import=True)
        if report.get("baseline_mode") != "user_import" or session is None:
            raise DataError("Для ручного подтверждения нужен итоговый отчёт по внесённому списку.")
        person = next((p for p in session["cancelled"] if p["student_id"] == student_id), None)
        if person is None:
            raise DataError("Этот участник не входит в список возможных отмен выбранного отчёта.")
        key = incident_key(session, student_id)
        with self.locked():
            state = self.load()
            if key in state["confirmed_cases"] or key in state["dismissed"]:
                return
            case = {field: session[field] for field in
                    ("training_id", "group_id", "training_start", "checked_at")}
            state["confirmed_cases"][key] = dict(case, person=person, source="confirmed_import",
                                                confirmed_at=self.config.now().isoformat())
            self.save(state, "confirm_import", "Screenshot case explicitly confirmed", {"incident_key": key})

    def sync_history(self):
        """Backfill old final reports; latest comparison wins and dismissals stay dismissed."""
        sessions = []
        for path in sorted((self.config.root / "reports").glob("*/report.json")):
            try:
                session = report_session(json.loads(path.read_text(encoding="utf-8")))
                if session is not None:
                    sessions.append(session)
            except (OSError, ValueError, TypeError, AttributeError, DataError):
                LOG.warning("Журнал отмен: сохранённый отчёт не прошёл проверку и пропущен.")
        with self.locked():
            state = self.load()
            changed = False
            for session in sessions:
                changed = self.merge(state, session) or changed
            if changed:
                self.save(state, "backfill", "Saved final reports imported")

    @staticmethod
    def people(state):
        people = {}
        dismissed = set(state["dismissed"])
        seen = set()

        def add(session, person, source):
            key = incident_key(session, person["student_id"])
            if key in dismissed or key in seen:
                return
            seen.add(key)
            entry = people.setdefault(person["student_id"], dict(person, incidents=[]))
            entry["full_name"] = person["full_name"]
            entry["incidents"].append({"key": key, "training_start": session["training_start"], "source": source})

        for session in sorted(state["sessions"].values(), key=lambda s: parse_datetime(s["checked_at"])):
            for person in session["cancelled"]:
                add(session, person, "scheduled")
        for case in state.get("confirmed_cases", {}).values():
            add(case, case["person"], case["source"])
        for entry in people.values():
            entry["incidents"].sort(key=lambda incident: parse_datetime(incident["training_start"]), reverse=True)
            entry["count"] = len(entry["incidents"])
            keys = sorted(incident["key"] for incident in entry["incidents"])
            entry["revision"] = hashlib.sha256(json.dumps(keys).encode()).hexdigest()[:16]
        return sorted(people.values(), key=lambda p: (-p["count"], p["full_name"].casefold(), p["student_id"]))

    def rows(self):
        with self.locked():
            return self.people(self.load())

    def dismiss(self, student_id, revision):
        """Forgive current incidents only; a stale button cannot erase new incidents."""
        return self.reduce(student_id, revision, clear=True)

    def decrement(self, student_id, revision):
        """Forgive the oldest current case; zero-count people are omitted automatically."""
        return self.reduce(student_id, revision, clear=False)

    def reduce(self, student_id, revision, clear):
        positive_id(student_id)
        with self.locked():
            state = self.load()
            person = next((p for p in self.people(state) if p["student_id"] == student_id), None)
            if person is None:
                return "Запись уже удалена."
            if person["revision"] != revision:
                return "Список изменился. Проверь новые записи и нажми кнопку ещё раз."
            incidents = person["incidents"] if clear else person["incidents"][-1:]
            keys = {incident["key"] for incident in incidents}
            cases = []
            for incident in incidents:
                key = incident["key"]
                if incident["source"] == "confirmed_import":
                    cases.append(state["confirmed_cases"][key])
                else:
                    session = state["sessions"][key.rsplit(":", 1)[0]]
                    case = {field: session[field] for field in
                            ("training_id", "group_id", "training_start", "checked_at")}
                    cases.append(dict(case, person={"student_id": student_id, "full_name": person["full_name"]},
                                      source="scheduled"))
            state["undo_history"].append({"id": secrets.token_hex(8),
                                          "created_at": self.config.now().isoformat(),
                                          "kind": "dismiss" if clear else "decrement", "cases": cases})
            state["undo_history"] = state["undo_history"][-20:]
            dismissed = set(state["dismissed"]) | keys
            state["dismissed"] = sorted(dismissed)
            for key in keys:
                state["confirmed_cases"].pop(key, None)
            self.save(state, "dismiss" if clear else "decrement",
                      "Owner cleared all current cases" if clear else "Owner forgave the oldest case",
                      {"action_id": state["undo_history"][-1]["id"], "incident_keys": sorted(keys)})
            remaining = person["count"] - len(incidents)
            if clear:
                return "Удалено из журнала: %s. Новые отмены будут учитываться." % person["count"]
            if not remaining:
                return "Счётчик: 0. Участник убран из списка."
            return "Счётчик: %s → %s. Списан самый ранний случай." % (person["count"], remaining)

    def latest_undo(self):
        with self.locked():
            history = self.load()["undo_history"]
            return history[-1]["id"] if history else None

    def undo(self, expected_id=None, request_id=None):
        """Restore one action, preserving new cases and newer site corrections."""
        with self.locked():
            state = self.load()
            if request_id and request_id in state["undo_results"]:
                return state["undo_results"][request_id]
            if not state["undo_history"]:
                return "Нет действий для отмены. Можно отменить последние 20 списаний и удалений."
            action = state["undo_history"][-1]
            if expected_id is not None and expected_id != action["id"]:
                return "Эта кнопка устарела. Используй /undo или новую кнопку под списком."
            dismissed = set(state["dismissed"])
            restored, corrected = 0, 0
            for case in action["cases"]:
                person = case["person"]
                key = incident_key(case, person["student_id"])
                if case["source"] == "confirmed_import":
                    state["confirmed_cases"][key] = case
                else:
                    current = state["sessions"].get(session_key(case))
                    if current is None or not any(p["student_id"] == person["student_id"]
                                                  for p in current["cancelled"]):
                        corrected += 1
                        continue
                dismissed.discard(key)
                restored += 1
            state["dismissed"] = sorted(dismissed)
            state["undo_history"].pop()
            answer = "↩️ Последнее действие отменено. Восстановлено случаев: %s." % restored
            if corrected:
                answer += " Уже исправлены на сайте и не восстановлены: %s." % corrected
            if request_id:
                state["undo_results"][request_id] = answer
                state["undo_results"] = dict(list(state["undo_results"].items())[-50:])
            self.save(state, "undo", "Owner undid the latest counter edit",
                      {"action_id": action["id"], "restored": restored, "corrected": corrected}, request_id)
            return answer
