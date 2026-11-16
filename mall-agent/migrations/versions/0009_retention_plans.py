"""Persist bounded, policy-bound retention plans and progress."""

from alembic import op

revision = "0009"
down_revision = "0008"


def upgrade():
    op.execute("""CREATE TABLE ag_retention_plan (
        id uuid PRIMARY KEY, created_by bigint, proposed_settings jsonb NOT NULL,
        settings_version bigint NOT NULL, settings_sha256 char(64) NOT NULL,
        cutoff timestamptz NOT NULL, expires_at timestamptz NOT NULL,
        preview_job_id uuid REFERENCES ag_job(id), execution_job_id uuid REFERENCES ag_job(id),
        summary jsonb NOT NULL DEFAULT '{}'::jsonb, progress jsonb NOT NULL DEFAULT '{}'::jsonb,
        scheduled boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX ix_retention_plan_due ON ag_retention_plan(created_at) WHERE scheduled")


def downgrade():
    raise RuntimeError("Use forward migrations; preserve retention and erasure evidence")
