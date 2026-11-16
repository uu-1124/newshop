"""Persist bounded visual observations without storing raw images in graph state."""

from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade():
    op.execute("""CREATE TABLE ag_image_observation (
        run_id uuid NOT NULL REFERENCES ag_run(id),
        attachment_id uuid NOT NULL REFERENCES ag_attachment(id),
        profile_id uuid NOT NULL,
        profile_version bigint NOT NULL,
        summary text NOT NULL CHECK (length(summary) BETWEEN 1 AND 3000),
        content_sha256 char(64) NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (run_id,attachment_id),
        FOREIGN KEY (profile_id,profile_version) REFERENCES ag_model_profile_version(profile_id,version_no)
    )""")


def downgrade():
    raise RuntimeError("Preserve image observation history; use a forward migration")
