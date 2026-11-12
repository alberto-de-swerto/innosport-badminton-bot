"""Private, deduplicated register of possible late cancellations."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import logging
import re

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
    def __init__(self, config):
        self.config = config
        self.path = config.root / ".state" / "violations.json"

    @contextmanager
    def locked(self):
        private_dir(self.path.parent)
        with (self.path.parent / "violations.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def load(self):
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
            return state
        except FileNotFoundError:
            return {"schema_version": 1, "sessions": {}, "dismissed": [], "confirmed_cases": {}}
        except (OSError, ValueError, KeyError, TypeError, AttributeError, DataError):
            raise DataError("Не удалось прочитать журнал поздних отмен. Данные не перезаписаны.") from None

    @staticmethod
    def merge(state, session):
        key = session_key(session)
        previous = state["sessions"].get(key)
        if previous and parse_datetime(previous["checked_at"]) >= parse_datetime(session["checked_at"]):
            return False
        dismissed = set(state["dismissed"])
        session = dict(session, cancelled=[p for p in session["cancelled"]
                                         if incident_key(session, p["student_id"]) not in dismissed])
        state["sessions"][key] = session
        return True

    def record(self, report):
        session = report_session(report)
        if session is None:
            return
        with self.locked():
            state = self.load()
            if self.merge(state, session):
                write_json(self.path, state)

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
            write_json(self.path, state)

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
                write_json(self.path, state)

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
            dismissed = set(state["dismissed"]) | keys
            state["dismissed"] = sorted(dismissed)
            for session in state["sessions"].values():
                session["cancelled"] = [p for p in session["cancelled"] if incident_key(session, p["student_id"]) not in keys]
            for key in keys:
                state["confirmed_cases"].pop(key, None)
            write_json(self.path, state)
            remaining = person["count"] - len(incidents)
            if clear:
                return "Удалено из журнала: %s. Новые отмены будут учитываться." % person["count"]
            if not remaining:
                return "Счётчик: 0. Участник убран из списка."
            return "Счётчик: %s → %s. Списан самый ранний случай." % (person["count"], remaining)
