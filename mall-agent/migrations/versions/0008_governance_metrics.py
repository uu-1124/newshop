"""Persist request metrics and immutable model metering/reconciliation evidence."""

from alembic import op

revision = "0008"
down_revision = "0007"


def upgrade():
    op.execute("""CREATE TABLE ag_request_metric (
        id uuid PRIMARY KEY, request_id uuid NOT NULL, operation varchar(100) NOT NULL,
        method varchar(8) NOT NULL, status_code integer NOT NULL CHECK(status_code BETWEEN 100 AND 599),
        duration_ms bigint NOT NULL CHECK(duration_ms>=0), created_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX ix_request_metric_time ON ag_request_metric(created_at DESC,operation)")
    # Reservation happens before the supplier call, so no FK to the later call row.
    op.execute("""CREATE TABLE ag_model_meter (
        call_id uuid PRIMARY KEY, actor_kind varchar(16) NOT NULL, actor_id bigint,
        pricing jsonb NOT NULL, usage jsonb NOT NULL DEFAULT '{}'::jsonb,
        usage_state varchar(16) NOT NULL DEFAULT 'PENDING'
          CHECK(usage_state IN ('PENDING','REPORTED','UNKNOWN','RECONCILED')),
        currency varchar(3), estimated_cost numeric(20,6) CHECK(estimated_cost>=0),
        statement_ref varchar(160), reconciled_by bigint, reconciled_at timestamptz,
        version bigint NOT NULL DEFAULT 1 CHECK(version>0),
        created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute(
        "CREATE INDEX ix_model_meter_unknown ON ag_model_meter(created_at,call_id) WHERE usage_state IN ('UNKNOWN','PENDING')"
    )


def downgrade():
    raise RuntimeError("Use forward migrations; preserve metrics and billing evidence")
