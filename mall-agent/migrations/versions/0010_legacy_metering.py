"""Expose historical unknown reservations in the new audited reconciliation UI."""

from alembic import op

revision = "0010"
down_revision = "0009"


def upgrade():
    op.execute("""INSERT INTO ag_model_meter(call_id,actor_kind,actor_id,pricing,usage,usage_state,created_at)
        SELECT q.call_id,CASE WHEN c.member_id IS NULL THEN 'SYSTEM' ELSE 'MEMBER' END,c.member_id,
        '[]'::jsonb,'{}'::jsonb,'UNKNOWN',min(q.created_at)
        FROM ag_quota_reservation q LEFT JOIN ag_model_call mc ON mc.id=q.call_id
        LEFT JOIN ag_run r ON r.id=mc.run_id LEFT JOIN ag_conversation c ON c.id=r.conversation_id
        WHERE q.state='UNKNOWN' GROUP BY q.call_id,c.member_id ON CONFLICT(call_id) DO NOTHING""")


def downgrade():
    raise RuntimeError("Preserve historical unknown billing evidence")
