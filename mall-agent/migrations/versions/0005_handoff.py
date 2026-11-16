"""Preserve the public staff name on a sent message."""

from alembic import op

revision = "0005"
down_revision = "0004"


def upgrade():
    op.execute("ALTER TABLE ag_message ADD COLUMN sender_display_name varchar(80)")


def downgrade():
    raise RuntimeError("Use forward migrations; preserve reception history")
