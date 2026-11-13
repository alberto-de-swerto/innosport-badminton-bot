"""Normalized registry and an append-only application audit trail.

Revision ID: 0001_registry
Revises: None
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001_registry"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("registry_meta",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("initialized", sa.Boolean, nullable=False),
        sa.Column("dataset", sa.String(16), nullable=False),
        sa.Column("import_sha256", sa.String(64)),
        sa.CheckConstraint("id = 1"), sa.CheckConstraint("dataset IN ('live', 'demo')"))
    op.create_table("registry_students",
        sa.Column("student_id", sa.BigInteger, primary_key=True),
        sa.Column("full_name", sa.Text, nullable=False), sa.CheckConstraint("student_id > 0"))
    op.create_table("registry_sessions",
        sa.Column("group_id", sa.BigInteger, primary_key=True),
        sa.Column("training_id", sa.BigInteger, primary_key=True),
        sa.Column("training_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("group_id > 0 AND training_id > 0"), sa.CheckConstraint("checked_at >= training_start"))
    op.create_table("registry_cases",
        sa.Column("group_id", sa.BigInteger, primary_key=True),
        sa.Column("training_id", sa.BigInteger, primary_key=True),
        sa.Column("student_id", sa.BigInteger, sa.ForeignKey("registry_students.student_id"), primary_key=True),
        sa.Column("full_name", sa.Text, nullable=False),
        sa.ForeignKeyConstraint(["group_id", "training_id"], ["registry_sessions.group_id", "registry_sessions.training_id"], ondelete="CASCADE"))
    op.create_table("registry_confirmed_cases",
        sa.Column("group_id", sa.BigInteger, primary_key=True),
        sa.Column("training_id", sa.BigInteger, primary_key=True),
        sa.Column("student_id", sa.BigInteger, sa.ForeignKey("registry_students.student_id"), primary_key=True),
        sa.Column("full_name", sa.Text, nullable=False),
        sa.Column("training_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("group_id > 0 AND training_id > 0"), sa.CheckConstraint("checked_at >= training_start"))
    op.create_table("registry_dismissals", sa.Column("incident_key", sa.Text, primary_key=True),
        sa.CheckConstraint("incident_key ~ '^[1-9][0-9]*:[1-9][0-9]*:[1-9][0-9]*$'"))
    op.create_table("registry_undo_actions",
        sa.Column("position", sa.Integer, primary_key=True),
        sa.Column("action_id", sa.String(16), unique=True, nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cases", JSONB, nullable=False), sa.CheckConstraint("kind IN ('decrement', 'dismiss')"))
    op.create_table("registry_undo_results", sa.Column("position", sa.Integer, primary_key=True),
        sa.Column("request_id", sa.Text, unique=True, nullable=False), sa.Column("result", sa.Text, nullable=False))
    op.create_table("registry_audit_events",
        sa.Column("sequence", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("event_id", sa.String(32), unique=True, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.Text, nullable=False), sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text, nullable=False), sa.Column("request_id", sa.Text),
        sa.Column("changes", JSONB, nullable=False), sa.Column("details", JSONB, nullable=False))
    op.create_index("ix_registry_audit_occurred", "registry_audit_events", ["occurred_at"])
    op.execute("INSERT INTO registry_meta (id, initialized, dataset) VALUES (1, false, 'live')")


def downgrade():
    # Application rollback must preserve data. Destructive schema rollback is explicit.
    for name in ("registry_audit_events", "registry_undo_results", "registry_undo_actions",
                 "registry_dismissals", "registry_confirmed_cases", "registry_cases",
                 "registry_sessions", "registry_students", "registry_meta"):
        op.drop_table(name)
