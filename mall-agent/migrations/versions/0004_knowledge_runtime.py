"""Knowledge build ownership, immutable evidence and cited source references."""

from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade():
    op.execute("""ALTER TABLE ag_kb_version
        ADD COLUMN change_note varchar(500) NOT NULL DEFAULT '',
        ADD COLUMN parse_report jsonb,
        ADD COLUMN parse_job_id uuid REFERENCES ag_job(id),
        ADD COLUMN index_job_id uuid REFERENCES ag_job(id)""")
    op.execute("""CREATE TABLE ag_kb_citation (
        message_id uuid NOT NULL REFERENCES ag_message(id),
        document_id uuid NOT NULL REFERENCES ag_kb_document(id),
        version_id uuid NOT NULL,
        chunk_id uuid NOT NULL,
        content_sha256 char(64) NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(message_id,version_id,chunk_id),
        FOREIGN KEY(document_id,version_id) REFERENCES ag_kb_version(document_id,id),
        FOREIGN KEY(version_id,chunk_id) REFERENCES ag_kb_chunk(version_id,id))""")
    op.execute("CREATE INDEX ix_kb_citation_source ON ag_kb_citation(version_id,chunk_id)")


def downgrade():
    raise RuntimeError("Use forward migrations; preserve published sources and citations")
