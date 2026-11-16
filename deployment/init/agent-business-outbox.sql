CREATE TABLE IF NOT EXISTS oms_agent_business_outbox (
  event_key varchar(100) NOT NULL PRIMARY KEY,
  order_id bigint NOT NULL,
  event_type varchar(32) NOT NULL,
  state varchar(16) NOT NULL DEFAULT 'PENDING',
  due_at datetime(6) NOT NULL,
  next_attempt_at datetime(6) NOT NULL,
  lease_owner varchar(80) NULL,
  lease_until datetime(6) NULL,
  fencing_token bigint NOT NULL DEFAULT 0,
  attempts int NOT NULL DEFAULT 0,
  published_at datetime(6) NULL,
  consumed_at datetime(6) NULL,
  last_error_code varchar(80) NULL,
  created_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  UNIQUE KEY uq_business_outbox_order(order_id,event_type),
  KEY ix_business_outbox_due(state,next_attempt_at,lease_until)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
