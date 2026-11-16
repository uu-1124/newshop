"""Persist immutable Java cart quotes next to action parameters."""

from alembic import op

revision = "0006"
down_revision = "0005"


def upgrade():
    op.execute("ALTER TABLE ag_action ADD COLUMN quote_snapshot jsonb")
    op.execute("ALTER TABLE ag_action ADD COLUMN message_id uuid REFERENCES ag_message(id)")


def downgrade():
    raise RuntimeError("Use forward migrations; preserve confirmed operation evidence")
