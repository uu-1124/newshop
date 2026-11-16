"""Durable job wakeups and worker liveness; original tables remain forward compatible."""
from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade():
    op.execute("ALTER TABLE ag_job DROP CONSTRAINT ag_job_job_type_check")
    op.execute("""ALTER TABLE ag_job ADD CONSTRAINT ag_job_job_type_check CHECK (job_type IN
        ('KB_PARSE','KB_INDEX','MODEL_COMPATIBILITY','RETENTION_PREVIEW','RETENTION_EXECUTE',
        'BACKUP_VERIFY','RESTORE_VERIFY','ATTACHMENT_DELETE','ACTION_RECONCILE','RUNTIME_CHECK'))""")
    op.execute("""CREATE TABLE ag_job_wakeup (
        id uuid PRIMARY KEY, job_id uuid NOT NULL REFERENCES ag_job(id),
        job_version bigint NOT NULL, published_at timestamptz,
        publish_attempts integer NOT NULL DEFAULT 0,
        next_attempt_at timestamptz NOT NULL DEFAULT now(), created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(job_id,job_version))""")
    op.execute("CREATE INDEX ix_job_wakeup_pending ON ag_job_wakeup(next_attempt_at,id) WHERE published_at IS NULL")
    op.execute("""CREATE TABLE ag_worker_heartbeat (
        worker_id varchar(100) PRIMARY KEY, process_kind varchar(24) NOT NULL,
        state varchar(24) NOT NULL, last_seen_at timestamptz NOT NULL DEFAULT now(),
        details jsonb NOT NULL DEFAULT '{}'::jsonb)""")
    op.execute("ALTER TABLE ag_event_outbox ADD COLUMN next_publish_at timestamptz NOT NULL DEFAULT now()")


def downgrade():
    raise RuntimeError("Use forward migrations; preserve job history")
