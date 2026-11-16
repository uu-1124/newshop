"""Record prompt change notes without rewriting any historical content."""

from alembic import op

revision = "0007"
down_revision = "0006"


def upgrade():
    op.execute("ALTER TABLE ag_prompt_version ADD COLUMN change_note varchar(500) NOT NULL DEFAULT ''")


def downgrade():
    raise RuntimeError("Use forward migrations; preserve prompt history")
