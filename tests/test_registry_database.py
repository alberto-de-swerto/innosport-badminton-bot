"""Real PostgreSQL integration tests; each test owns an isolated temporary schema."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import json
import os
import unittest
import uuid
from http.server import ThreadingHTTPServer
import threading
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest import mock

import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from registry_admin import migrate, normalized
from registry_database import RegistryDatabase, metadata
from sportsite import Config, DataError, write_json
from test_sportsite import TemporaryProject, START
from test_telegram_bot import SETTINGS, NOW, update
from test_violation_registry import report
import telegram_bot as bot
from violation_registry import ViolationRegistry
from demo_app import handler_for


TEST_URL = os.environ.get("INNOSPORT_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_URL, "Set INNOSPORT_TEST_DATABASE_URL for real PostgreSQL integration tests")
class DatabaseTests(TemporaryProject):
    def setUp(self):
        super().setUp()
        self.schema = "test_" + uuid.uuid4().hex
        self.admin = sa.create_engine(TEST_URL, poolclass=sa.pool.NullPool)
        with self.admin.begin() as connection:
            connection.execute(sa.text('CREATE SCHEMA "' + self.schema + '"'))
        self.addCleanup(self.drop_schema)
        write_json(self.root / ".state/database.json", {"url": TEST_URL, "schema": self.schema})
        self.json_config = self.config
        self.config = replace(self.config, registry_backend="postgresql")
        self.database = RegistryDatabase(self.config)
        self.database.upgrade()
        self.database.import_legacy(ViolationRegistry(self.json_config).load(), b"empty-registry")
        self.registry = ViolationRegistry(self.config, actor="telegram:77")

    def drop_schema(self):
        with self.admin.begin() as connection:
            connection.execute(sa.text('DROP SCHEMA "' + self.schema + '" CASCADE'))
        self.admin.dispose()

    def test_schema_matches_models_and_upgrade_is_repeatable(self):
        self.database.upgrade()
        with self.database.engine.begin() as connection:
            changes = compare_metadata(MigrationContext.configure(connection), metadata)
            self.assertEqual(changes, [])

    def test_same_domain_operations_match_legacy_json(self):
        legacy = ViolationRegistry(self.json_config)
        for operation in (lambda r: r.record(report()), lambda r: r.record(report()),
                lambda r: r.record(report(102, START + timedelta(days=2))),
                lambda r: r.decrement(2, r.rows()[0]["revision"]), lambda r: r.undo(request_id="test:undo"),
                lambda r: r.confirm_import(dict(report(103), baseline_mode="user_import"), 2),
                lambda r: r.dismiss(2, r.rows()[0]["revision"]),
                lambda r: r.record(report(102, START + timedelta(days=2), cancelled=[], checked=START + timedelta(days=2, hours=4))),
                lambda r: r.undo(request_id="test:undo2")):
            operation(legacy)
            operation(self.registry)
            self.assertEqual(self.registry.rows(), legacy.rows())
            self.assertEqual(self.registry.load()["dismissed"], legacy.load()["dismissed"])

    def test_audit_records_actor_reason_request_and_before_after_once(self):
        self.registry.record(report())
        registry = ViolationRegistry(self.config, actor="telegram:77", request_id="callback:example", reason="Excused by club leader")
        row = registry.rows()[0]
        registry.decrement(2, row["revision"])
        registry.decrement(2, row["revision"])
        events = registry.audit()
        event = events[0]
        self.assertEqual(event["actor"], "telegram:77")
        self.assertEqual(event["kind"], "decrement")
        self.assertEqual(event["reason"], "Excused by club leader")
        self.assertEqual(event["request_id"], "callback:example")
        self.assertEqual((event["changes"][0]["before"], event["changes"][0]["after"]), (1, 0))
        self.assertEqual(sum(e["kind"] == "decrement" for e in events), 1)
        self.registry.undo(request_id="undo:1")
        self.registry.undo(request_id="undo:1")
        self.assertEqual(sum(e["kind"] == "undo" for e in self.registry.audit()), 1)

    def test_fault_after_write_rolls_back_counters_and_audit_together(self):
        self.registry.record(report())
        before, audit = self.registry.load(), self.registry.audit()
        with self.assertRaises(RuntimeError):
            with self.registry.locked():
                state = self.registry.load()
                state["dismissed"] = ["17:101:2"]
                self.registry.save(state, "dismiss", "Injected crash")
                raise RuntimeError("process failed before commit")
        self.assertEqual(self.registry.load(), before)
        self.assertEqual(self.registry.audit(), audit)

    def test_multiple_workers_serialize_and_do_not_lose_cases(self):
        reports = [report(ident, START + timedelta(days=ident - 101)) for ident in range(101, 113)]
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(lambda data: ViolationRegistry(self.config).record(data), reports))
        self.assertEqual(self.registry.rows()[0]["count"], 12)
        self.assertEqual(sum(e["kind"] == "report" for e in self.registry.audit(100)), 12)

    def test_legacy_import_preserves_dismissals_confirmations_undo_and_source(self):
        # Reset only this isolated schema to simulate a new installation.
        with self.admin.begin() as connection:
            connection.execute(sa.text('DROP SCHEMA "' + self.schema + '" CASCADE'))
            connection.execute(sa.text('CREATE SCHEMA "' + self.schema + '"'))
        legacy = ViolationRegistry(self.json_config)
        legacy.record(report())
        legacy.confirm_import(dict(report(103), baseline_mode="user_import"), 2)
        legacy.decrement(2, legacy.rows()[0]["revision"])
        original = legacy.path.read_bytes()
        self.assertTrue(migrate(self.json_config, activate=True, config_path=self.root / "config.json"))
        self.assertEqual(Config.load(self.root / "config.json").registry_backend, "postgresql")
        self.assertEqual(normalized(self.registry.load()), normalized(legacy.load()))
        self.assertEqual(legacy.path.read_bytes(), original)
        self.registry.undo(request_id="after-migration")
        self.assertEqual(self.registry.rows()[0]["count"], 2)
        self.assertFalse(migrate(self.json_config))
        self.assertEqual(self.registry.rows()[0]["count"], 2)
        legacy.record(report(104))
        with self.assertRaises(DataError):
            migrate(self.json_config)

    def test_database_failure_does_not_use_stale_json_or_leak_url(self):
        ViolationRegistry(self.json_config).record(report())
        self.assertEqual(self.registry.rows(), [])
        write_json(self.root / ".state/database.json", {"url": "postgresql+psycopg://fake:private-secret@127.0.0.1:1/missing"})
        with self.assertRaises(DataError) as caught:
            ViolationRegistry(self.config).rows()
        self.assertNotIn("private-secret", str(caught.exception))
        self.assertNotIn("127.0.0.1", str(caught.exception))
        self.assertEqual(ViolationRegistry(self.json_config).rows()[0]["count"], 1)

    def test_uninitialized_or_wrong_revision_refuses_to_run(self):
        with self.database.engine.begin() as connection:
            connection.execute(sa.text("UPDATE registry_meta SET initialized=false"))
        with self.assertRaises(DataError):
            self.registry.rows()
        with self.database.engine.begin() as connection:
            connection.execute(sa.text("UPDATE alembic_version SET version_num='unsupported'"))
        with self.assertRaises(DataError):
            self.registry.rows()

    def test_history_is_owner_only_and_localized(self):
        self.registry.record(report())
        api = mock.Mock()
        settings = dict(SETTINGS, language="en")
        bot.handle_update(self.config, settings, api, update(text="/history", user=88), now=NOW)
        api.send.assert_not_called()
        bot.handle_update(self.config, settings, api, update(text="/history"), now=NOW)
        text = api.send.call_args.args[1]
        self.assertIn("Recent counter changes", text)
        self.assertIn("telegram:77", text)
        self.assertIn("0 → 1", text)

    def test_malformed_json_never_activates_postgres(self):
        write_json(self.root / ".state/violations.json", {"schema_version": 999})
        with self.assertRaises(DataError):
            migrate(self.json_config, activate=True, config_path=self.root / "config.json")
        self.assertEqual(Config.load(self.root / "config.json").registry_backend, "json")

    def test_public_demo_cannot_read_live_database_and_bot_rejects_demo_dataset(self):
        self.registry.record(report())
        with self.assertRaises(DataError):
            ViolationRegistry(replace(self.config, registry_dataset="demo")).rows()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.config))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(HTTPError) as caught:
                urlopen("http://127.0.0.1:%s/api/violations" % server.server_port)
            self.assertEqual(caught.exception.code, 503)
            self.assertNotIn("full_name", caught.exception.read().decode())
            caught.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_activation_refuses_running_worker(self):
        import fcntl
        path = self.root / ".state/telegram.lock"
        with path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaisesRegex(Exception, "останови"):
                migrate(self.json_config, activate=True, config_path=self.root / "config.json")
        self.assertEqual(Config.load(self.root / "config.json").registry_backend, "json")

    def test_sql_constraint_failure_rolls_back_and_sanitizes_error(self):
        self.registry.record(report())
        before, audit = self.registry.load(), self.registry.audit()
        with self.assertRaises(DataError):
            with self.registry.locked():
                state = self.registry.load()
                state["sessions"]["17:101"]["cancelled"][0]["student_id"] = -1
                self.registry.save(state, "report", "invalid update")
        self.assertEqual(self.registry.load(), before)
        self.assertEqual(self.registry.audit(), audit)
