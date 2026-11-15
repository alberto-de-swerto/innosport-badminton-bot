"""Classify no-shows and re-evaluate the latest saved reports once.

Revision ID: 0002_no_shows
Revises: 0001_registry
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_no_shows"
down_revision = "0001_registry"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("registry_sessions", sa.Column("policy_version", sa.Integer, nullable=False, server_default="1"))
    for table, constraint in (("registry_cases", "ck_registry_cases_kind"),
                              ("registry_confirmed_cases", "ck_registry_confirmed_kind")):
        op.add_column(table, sa.Column("violation_kind", sa.String(24), nullable=False, server_default="late_cancellation"))
        op.create_check_constraint(constraint, table, "violation_kind IN ('late_cancellation', 'no_show')")


def downgrade():
    for table, constraint in (("registry_cases", "ck_registry_cases_kind"),
                              ("registry_confirmed_cases", "ck_registry_confirmed_kind")):
        op.drop_constraint(constraint, table, type_="check")
        op.drop_column(table, "violation_kind")
    op.drop_column("registry_sessions", "policy_version")
