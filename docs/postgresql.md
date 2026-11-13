# PostgreSQL register and audit history

The register supports PostgreSQL 15+ through SQLAlchemy Core and psycopg. Existing
installations keep the JSON backend until an explicit, verified migration.
Snapshots, comparison reports, cookies, bot credentials and delivery journals
remain private local files. PostgreSQL stores the cancellation register only.

## Configure and migrate

Create a dedicated database and non-superuser application role using your local
PostgreSQL administrator. For example, `createuser --pwprompt innosport_bot` and
`createdb --owner=innosport_bot innosport_badminton`. Do not reuse the demo database.

Install `requirements.txt` in both the source and installed runtime environments.
The commands below follow the same active config marker as the sports CLI:

```sh
.venv/bin/python registry_admin.py configure
.venv/bin/python registry_admin.py upgrade
```

`configure` asks for the database password with hidden input and writes a mode-600
`.state/database.json`. Optional arguments are `--host`, `--port`, `--database`
and `--user`. Alternatively set `INNOSPORT_DATABASE_URL` for a process. Never put
the URL in GitHub variables, a command argument, `config.example.json`, or logs.

Stop the existing Telegram worker and scheduler before switching storage. On macOS:

```sh
launchctl bootout "gui/$(id -u)/university.innopolis.badminton-telegram"
launchctl bootout "gui/$(id -u)/university.innopolis.badminton-snapshots"
.venv/bin/python registry_admin.py migrate --activate
```

The migration:

1. Validates the original JSON, including confirmed imports, dismissals and undo.
2. Upgrades the database with versioned Alembic migrations.
3. Imports the state and verifies its round trip inside the same transaction.
4. Records the source SHA-256 and a migration audit event.
5. Switches `registry_backend` to `postgresql` only after verification.

The JSON source is preserved. Repeating the same import is a no-op and does not
replace later database edits. Importing a different JSON into an initialized
database is refused. Activation checks the worker locks; old workers must not
continue writing JSON after the switch. Restart the installed LaunchAgents after
copying the updated modules, `migrations/`, and dependencies into the runtime.

Do not switch back to the preserved original JSON after new database edits.
Export the current state first with `registry_admin.py export /private/path.json`,
stop workers, and verify that export before using it as a recovery source.

## Data model and transactions

`registry_students` contains stable IDs; `registry_sessions` and `registry_cases`
keep session-specific names and possible cancellations. Separate tables store
confirmed screenshot cases, dismissal identifiers, the last 20 undo actions and
the last 50 undo responses. Counts are derived from active cases rather than
maintained as a second, potentially inconsistent number.

Every operation acquires `SELECT ... FOR UPDATE` on the registry's singleton
metadata row. This serializes updates across processes and hosts. Domain state
and the audit event commit together; a crash or failed constraint rolls both back.
The state tables are currently replaced inside that transaction, a deliberate
tradeoff for a small club. A higher-volume system would need incremental writes
and narrower locks. Foreign keys, positive-ID checks and unique keys are enforced
by PostgreSQL. Connection and SQL errors are sanitized and never trigger fallback
to stale JSON. Database outages leave snapshot files and notification scheduling
independent of register updates.

## Audit history

Use `/history` in the paired private Telegram chat, or `registry_admin.py audit`
locally. Events record timestamp, actor, operation, reason, request identifier,
changed student IDs/names, counts before/after, and added/removed incident keys.
Telegram actions use `telegram:<user_id>`; reports, backfills and imports have
explicit system identities. The initial import cannot recover the authors of
historical actions. Reasons default to the operation's purpose; repository callers
can pass a specific `reason` for a manual decision.

The application only appends audit events; it does not claim tamper-proof history
against a database owner. Audit rows retain personal data even when a counter is
cleared. Protect database access and backups accordingly. The original JSON is a
migration backup, not an automatically refreshed copy of PostgreSQL.

## Integration tests

Create an isolated test database whose user may create/drop schemas, then run:

```sh
INNOSPORT_TEST_DATABASE_URL=postgresql+psycopg:///innosport_registry_test \
  .venv/bin/python -m unittest discover -s tests -p test_registry_database.py -v
```

Each test owns a random schema and removes it afterwards. CI runs against a real
PostgreSQL 16 service. Tests cover JSON parity, migration and repeat safety,
concurrent workers, audit attribution, rollback, dataset separation and history
authorization. Without the test URL, those integration tests are explicitly skipped.
