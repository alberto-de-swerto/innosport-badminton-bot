"""Explicit PostgreSQL schema upgrades, verified JSON import and audit export."""

import argparse
from contextlib import ExitStack, contextmanager
from dataclasses import replace
import fcntl
import getpass
import json
import os
from pathlib import Path

from registry_database import RegistryDatabase, normalized
from sportsite import Config, SportError, default_config_path, write_json
from violation_registry import ViolationRegistry


@contextmanager
def stopped_workers(config, required):
    with ExitStack() as stack:
        if required:
            for name in ("telegram.lock", "scheduler.lock"):
                path = config.root / ".state" / name
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                stream = stack.enter_context(path.open("a"))
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise SportError("Перед переключением останови Telegram-бота и планировщик.") from None
        yield


def migrate(config, activate=False, config_path=None):
    legacy = ViolationRegistry(replace(config, registry_backend="json"))
    database = RegistryDatabase(config)
    database.upgrade()
    # This lock also coordinates an old JSON worker until the operator switches it off.
    with stopped_workers(config, activate), legacy.locked():
        state = legacy.load_json()
        source = legacy.path.read_bytes() if legacy.path.exists() else b"empty-registry"
        changed = database.import_legacy(state, source)
        with database.transaction() as connection:
            actual = database.load(connection)
            # A repeated import must never compare/replace database edits with stale JSON.
            if changed and normalized(actual) != normalized(state):
                raise SportError("Проверка переноса не совпала с исходным JSON. Переключение не выполнено.")
        if activate:
            if not changed and normalized(actual) != normalized(state):
                raise SportError("Журнал PostgreSQL уже изменён после переноса. Проверь данные перед переключением.")
            if config_path is None:
                raise SportError("Для переключения нужен путь config.json.")
            raw = json.loads(config_path.read_text())
            raw["registry_backend"] = "postgresql"
            write_json(config_path, raw)
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=default_config_path())
    sub = parser.add_subparsers(dest="command", required=True)
    configure = sub.add_parser("configure")
    configure.add_argument("--host", default="127.0.0.1")
    configure.add_argument("--port", type=int, default=5432)
    configure.add_argument("--database", default="innosport_badminton")
    configure.add_argument("--user", default="innosport_bot")
    sub.add_parser("upgrade")
    migration = sub.add_parser("migrate")
    migration.add_argument("--activate", action="store_true", help="switch config after verification; stop old workers first")
    sub.add_parser("audit")
    export = sub.add_parser("export")
    export.add_argument("destination", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        config = Config.load(args.config)
        if args.command == "configure":
            from sqlalchemy.engine import URL
            password = getpass.getpass("PostgreSQL password (hidden): ")
            url = URL.create("postgresql+psycopg", username=args.user, password=password,
                             host=args.host, port=args.port, database=args.database)
            write_json(config.root / ".state/database.json", {"url": url.render_as_string(hide_password=False)})
            print("Private PostgreSQL settings saved; the active backend was not changed.")
            return 0
        database = RegistryDatabase(config)
        if args.command == "upgrade":
            database.upgrade()
            print("PostgreSQL schema is up to date.")
        elif args.command == "migrate":
            changed = migrate(config, args.activate, args.config)
            print("JSON imported and verified; source preserved." if changed else "Already imported; database changes preserved.")
        elif args.command == "audit":
            print(json.dumps(database.audit(100), ensure_ascii=False, default=str, indent=2))
        else:
            with database.transaction() as connection:
                write_json(args.destination, database.load(connection))
            print("Private JSON export saved.")
        return 0
    except SportError as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
