"""Immutable copy of the reviewed 29-table design; checkpoint tables use the library setup."""

from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None


def upgrade():
    ddl = (Path(__file__).parents[1] / "sql" / "0001.sql").read_text(encoding="utf-8")
    # The version runner owns the transaction; strip only the source's outer transaction markers.
    ddl = ddl.replace("\nBEGIN;", "").replace("\nCOMMIT;", "")
    op.get_bind().connection.driver_connection.execute(ddl, prepare=False)


def downgrade():
    raise RuntimeError(
        "Non-destructive baseline: restore an isolated backup instead of dropping business tables"
    )
