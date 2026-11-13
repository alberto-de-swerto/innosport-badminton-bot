"""Transactional PostgreSQL repository; never fall back to a stale JSON register."""

from contextlib import contextmanager
from datetime import timezone
import hashlib
import json
import os
import re
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from sportsite import DataError, ROOT, parse_datetime


REVISION = "0001_registry"
metadata = sa.MetaData()
meta = sa.Table("registry_meta", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("initialized", sa.Boolean, nullable=False),
    sa.Column("dataset", sa.String(16), nullable=False),
    sa.Column("import_sha256", sa.String(64)),
    sa.CheckConstraint("id = 1"),
    sa.CheckConstraint("dataset IN ('live', 'demo')"))
students = sa.Table("registry_students", metadata,
    sa.Column("student_id", sa.BigInteger, primary_key=True),
    sa.Column("full_name", sa.Text, nullable=False),
    sa.CheckConstraint("student_id > 0"))
sessions = sa.Table("registry_sessions", metadata,
    sa.Column("group_id", sa.BigInteger, primary_key=True),
    sa.Column("training_id", sa.BigInteger, primary_key=True),
    sa.Column("training_start", sa.DateTime(timezone=True), nullable=False),
    sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("group_id > 0 AND training_id > 0"),
    sa.CheckConstraint("checked_at >= training_start"))
cases = sa.Table("registry_cases", metadata,
    sa.Column("group_id", sa.BigInteger, primary_key=True),
    sa.Column("training_id", sa.BigInteger, primary_key=True),
    sa.Column("student_id", sa.BigInteger, sa.ForeignKey("registry_students.student_id"), primary_key=True),
    sa.Column("full_name", sa.Text, nullable=False),
    sa.ForeignKeyConstraint(["group_id", "training_id"],
                            ["registry_sessions.group_id", "registry_sessions.training_id"], ondelete="CASCADE"))
confirmed = sa.Table("registry_confirmed_cases", metadata,
    sa.Column("group_id", sa.BigInteger, primary_key=True),
    sa.Column("training_id", sa.BigInteger, primary_key=True),
    sa.Column("student_id", sa.BigInteger, sa.ForeignKey("registry_students.student_id"), primary_key=True),
    sa.Column("full_name", sa.Text, nullable=False),
    sa.Column("training_start", sa.DateTime(timezone=True), nullable=False),
    sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("group_id > 0 AND training_id > 0"),
    sa.CheckConstraint("checked_at >= training_start"))
dismissals = sa.Table("registry_dismissals", metadata,
    sa.Column("incident_key", sa.Text, primary_key=True),
    sa.CheckConstraint("incident_key ~ '^[1-9][0-9]*:[1-9][0-9]*:[1-9][0-9]*$'"))
undo_actions = sa.Table("registry_undo_actions", metadata,
    sa.Column("position", sa.Integer, primary_key=True),
    sa.Column("action_id", sa.String(16), unique=True, nullable=False),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("cases", JSONB, nullable=False),
    sa.CheckConstraint("kind IN ('decrement', 'dismiss')"))
undo_results = sa.Table("registry_undo_results", metadata,
    sa.Column("position", sa.Integer, primary_key=True),
    sa.Column("request_id", sa.Text, unique=True, nullable=False),
    sa.Column("result", sa.Text, nullable=False))
audit_events = sa.Table("registry_audit_events", metadata,
    sa.Column("sequence", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("event_id", sa.String(32), unique=True, nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("kind", sa.String(32), nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("request_id", sa.Text),
    sa.Column("changes", JSONB, nullable=False),
    sa.Column("details", JSONB, nullable=False))
sa.Index("ix_registry_audit_occurred", audit_events.c.occurred_at)


def connection_settings(config):
    """The URL lives in a private runtime file or an environment variable, never config.json."""
    try:
        path = config.root / ".state/database.json"
        settings = json.loads(path.read_text()) if path.exists() else {}
        url = os.environ.get("INNOSPORT_DATABASE_URL") or settings["url"]
        schema = settings.get("schema", "public")
        parsed = sa.engine.make_url(url)
        if parsed.drivername not in ("postgresql", "postgresql+psycopg"):
            raise ValueError()
        if not isinstance(schema, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
            raise ValueError()
        return parsed.set(drivername="postgresql+psycopg"), schema
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise DataError("Настройки PostgreSQL отсутствуют или повреждены. Проверь .state/database.json.") from None


def counter_changes(before, after):
    from violation_registry import ViolationRegistry
    old = {p["student_id"]: p for p in ViolationRegistry.people(before)}
    new = {p["student_id"]: p for p in ViolationRegistry.people(after)}
    changes = []
    for ident in sorted(old.keys() | new.keys()):
        previous, current = old.get(ident), new.get(ident)
        old_keys = {i["key"] for i in previous["incidents"]} if previous else set()
        new_keys = {i["key"] for i in current["incidents"]} if current else set()
        if old_keys != new_keys:
            changes.append({"student_id": ident, "full_name": (current or previous)["full_name"],
                            "before": len(old_keys), "after": len(new_keys),
                            "added": sorted(new_keys - old_keys), "removed": sorted(old_keys - new_keys)})
    return changes


def normalized(value):
    """Canonical domain state for verifying a migration before committing it."""
    if isinstance(value, dict):
        return {k: (parse_datetime(v).astimezone(timezone.utc).isoformat()
                    if k in ("training_start", "checked_at", "created_at", "confirmed_at")
                    else normalized(v)) for k, v in value.items()}
    if isinstance(value, list):
        items = [normalized(v) for v in value]
        return sorted(items, key=lambda p: p["student_id"]) if items and isinstance(items[0], dict) and "student_id" in items[0] else items
    return value


class RegistryDatabase:
    def __init__(self, config):
        self.config = config
        url, schema = connection_settings(config)
        self.engine = sa.create_engine(url, poolclass=NullPool, hide_parameters=True,
            connect_args={"connect_timeout": 5, "options": "-csearch_path=" + schema
                          + " -clock_timeout=10000 -cstatement_timeout=30000"})

    def upgrade(self):
        from alembic import command
        from alembic.config import Config as AlembicConfig
        migration = AlembicConfig()
        migration.set_main_option("script_location", str(ROOT / "migrations"))
        try:
            with self.engine.begin() as connection:
                connection.execute(sa.text("SELECT pg_advisory_xact_lock(1847319021)"))
                migration.attributes["connection"] = connection
                command.upgrade(migration, "head")
        except SQLAlchemyError:
            raise DataError("Не удалось обновить схему PostgreSQL. Подробности подключения скрыты.") from None

    @contextmanager
    def transaction(self, require_initialized=True, dataset=None):
        if dataset is None and require_initialized:
            dataset = self.config.registry_dataset
        try:
            with self.engine.begin() as connection:
                revision = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
                if revision != REVISION:
                    raise DataError("Версия схемы PostgreSQL не поддерживается. Выполни registry-admin.py upgrade.")
                row = connection.execute(sa.select(meta).where(meta.c.id == 1).with_for_update()).mappings().one()
                if require_initialized and not row["initialized"]:
                    raise DataError("Журнал PostgreSQL ещё не перенесён. Выполни registry-admin.py migrate.")
                if dataset is not None and row["dataset"] != dataset:
                    raise DataError("Тип базы не совпадает с режимом приложения. Доступ остановлен.")
                yield connection
        except SQLAlchemyError:
            raise DataError("PostgreSQL недоступен или операция не завершена. Изменения отменены; JSON не используется как запасной журнал.") from None

    def iso(self, value):
        return value.astimezone(self.config.timezone).isoformat()

    def load(self, connection):
        state = {"schema_version": 1, "sessions": {}, "dismissed": [], "confirmed_cases": {},
                 "undo_history": [], "undo_results": {}}
        for row in connection.execute(sa.select(sessions)).mappings():
            key = "%s:%s" % (row["group_id"], row["training_id"])
            state["sessions"][key] = {"group_id": row["group_id"], "training_id": row["training_id"],
                "training_start": self.iso(row["training_start"]), "checked_at": self.iso(row["checked_at"]), "cancelled": []}
        for row in connection.execute(sa.select(cases).order_by(cases.c.student_id)).mappings():
            state["sessions"]["%s:%s" % (row["group_id"], row["training_id"])]["cancelled"].append(
                {"student_id": row["student_id"], "full_name": row["full_name"]})
        for row in connection.execute(sa.select(confirmed)).mappings():
            case = {key: self.iso(row[key]) for key in ("training_start", "checked_at", "confirmed_at")}
            case.update(group_id=row["group_id"], training_id=row["training_id"], source="confirmed_import",
                        person={"student_id": row["student_id"], "full_name": row["full_name"]})
            state["confirmed_cases"]["%s:%s:%s" % (row["group_id"], row["training_id"], row["student_id"])] = case
        state["dismissed"] = sorted(connection.execute(sa.select(dismissals.c.incident_key)).scalars())
        for row in connection.execute(sa.select(undo_actions).order_by(undo_actions.c.position)).mappings():
            state["undo_history"].append({"id": row["action_id"], "kind": row["kind"],
                "created_at": self.iso(row["created_at"]), "cases": row["cases"]})
        for row in connection.execute(sa.select(undo_results).order_by(undo_results.c.position)).mappings():
            state["undo_results"][row["request_id"]] = row["result"]
        return state

    def save(self, connection, state, actor, kind, reason, metadata, request_id=None):
        before = self.load(connection)
        people = {}
        session_rows, case_rows, confirmed_rows = [], [], []
        for session in state["sessions"].values():
            identity = {k: session[k] for k in ("group_id", "training_id")}
            session_rows.append(dict(identity, training_start=parse_datetime(session["training_start"]),
                                     checked_at=parse_datetime(session["checked_at"])))
            for person in session["cancelled"]:
                people[person["student_id"]] = person
                case_rows.append(dict(identity, **person))
        for case in state["confirmed_cases"].values():
            people[case["person"]["student_id"]] = case["person"]
            confirmed_rows.append(dict(group_id=case["group_id"], training_id=case["training_id"],
                **case["person"], **{k: parse_datetime(case[k]) for k in ("training_start", "checked_at", "confirmed_at")}))
        if people:
            statement = insert(students).values(list(people.values()))
            connection.execute(statement.on_conflict_do_update(index_elements=[students.c.student_id],
                                set_={"full_name": statement.excluded.full_name}))
        # One locked transaction preserves the domain's existing read/modify/write semantics.
        # Audit rows are append-only and are never part of this replacement.
        for table in (cases, sessions, confirmed, dismissals, undo_actions, undo_results):
            connection.execute(table.delete())
        for table, rows in ((sessions, session_rows), (cases, case_rows), (confirmed, confirmed_rows),
                (dismissals, [{"incident_key": key} for key in state["dismissed"]]),
                (undo_actions, [{"position": i, "action_id": action["id"], "kind": action["kind"],
                    "created_at": parse_datetime(action["created_at"]), "cases": action["cases"]}
                    for i, action in enumerate(state["undo_history"])]),
                (undo_results, [{"position": i, "request_id": key, "result": value}
                               for i, (key, value) in enumerate(state["undo_results"].items())])):
            if rows:
                connection.execute(table.insert(), rows)
        connection.execute(audit_events.insert().values(event_id=uuid.uuid4().hex,
            occurred_at=self.config.now(), actor=actor, kind=kind, reason=reason, request_id=request_id,
            changes=counter_changes(before, state), details=metadata))

    def audit(self, limit=10):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DataError("Размер страницы истории должен быть от 1 до 100.")
        with self.transaction() as connection:
            rows = connection.execute(sa.select(audit_events).order_by(audit_events.c.sequence.desc()).limit(limit)).mappings()
            return [dict(row, occurred_at=self.iso(row["occurred_at"])) for row in rows]

    def import_legacy(self, state, source_bytes, dataset="live"):
        """Import once into an empty registry; repeats cannot replace newer database changes."""
        digest = hashlib.sha256(source_bytes).hexdigest()
        with self.transaction(require_initialized=False) as connection:
            row = connection.execute(sa.select(meta).where(meta.c.id == 1)).mappings().one()
            if row["initialized"]:
                if row["import_sha256"] == digest and row["dataset"] == dataset:
                    return False
                raise DataError("PostgreSQL уже содержит журнал. Повторный импорт других данных запрещён.")
            self.save(connection, state, "migration", "legacy_import",
                "Imported existing JSON; authors of historical edits are unknown",
                {"sha256": digest, "dataset": dataset})
            if normalized(self.load(connection)) != normalized(state):
                raise DataError("Проверка переноса JSON не совпала. Вся транзакция отменена.")
            connection.execute(meta.update().where(meta.c.id == 1).values(
                initialized=True, dataset=dataset, import_sha256=digest))
        return True
