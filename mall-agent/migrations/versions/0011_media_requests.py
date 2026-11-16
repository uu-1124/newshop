"""Durable member media requests and model accounting attribution."""

from alembic import op

revision = "0011"
down_revision = "0010"


def upgrade():
    op.execute("""CREATE TABLE ag_media_request (
        id uuid PRIMARY KEY, member_id bigint NOT NULL CHECK(member_id>0),
        kind varchar(8) NOT NULL CHECK(kind IN ('ASR','TTS')),
        idempotency_key uuid NOT NULL, request_hash char(64) NOT NULL, cache_key char(64),
        attachment_id uuid REFERENCES ag_attachment(id), message_id uuid REFERENCES ag_message(id),
        state varchar(16) NOT NULL DEFAULT 'RUNNING' CHECK(state IN ('RUNNING','SUCCEEDED','FAILED','UNKNOWN')),
        lease_owner varchar(100) NOT NULL, fencing_token bigint NOT NULL DEFAULT 1,
        lease_until timestamptz NOT NULL DEFAULT now()+interval '5 minutes',
        result jsonb, error_code varchar(80), created_at timestamptz NOT NULL DEFAULT now(),
        retention_until timestamptz NOT NULL DEFAULT now()+interval '7 days',
        UNIQUE(member_id,kind,idempotency_key),
        CHECK((kind='ASR' AND attachment_id IS NOT NULL AND message_id IS NULL)
           OR (kind='TTS' AND message_id IS NOT NULL AND attachment_id IS NULL)))""")
    op.execute("CREATE INDEX ix_media_retention ON ag_media_request(retention_until)")
    op.execute("ALTER TABLE ag_model_call ADD COLUMN media_request_id uuid REFERENCES ag_media_request(id)")
    op.execute(
        "ALTER TABLE ag_model_call ADD CONSTRAINT ck_model_one_source CHECK(num_nonnulls(run_id,job_id,media_request_id)<=1)"
    )


def downgrade():
    raise RuntimeError("Preserve media request and model accounting history")
