"""Migrations run through registry-admin.py, using a preconfigured connection."""
from alembic import context
from registry_database import metadata

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Use registry-admin.py upgrade; connection details are kept out of alembic.ini")
context.configure(connection=connection, target_metadata=metadata, compare_type=True)
with context.begin_transaction():
    context.run_migrations()
