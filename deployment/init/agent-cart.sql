-- ADD_CART and CART checkout. Apply explicitly; cancellation/after-sale actions remain inactive.
CREATE TABLE IF NOT EXISTS oms_agent_quote (
 quote_id char(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 member_id bigint NOT NULL, action_type varchar(24) NOT NULL,
 parameters_hash char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 parameters_json json NOT NULL, pricing_snapshot json NOT NULL,
 total_amount decimal(18,2) NOT NULL, currency char(3) NOT NULL DEFAULT 'CNY',
 checkout_fingerprint char(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
 rule_version varchar(80) NOT NULL, expires_at datetime(6) NOT NULL,
 created_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 KEY ix_quote_member(member_id,created_at),
 CONSTRAINT ck_quote_amount CHECK(total_amount>=0),
 CONSTRAINT ck_quote_action CHECK(action_type IN ('ADD_CART','CREATE_ORDER','CANCEL_ORDER','CREATE_RETURN'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS oms_agent_operation (
 operation_no char(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 member_id bigint NOT NULL, action_type varchar(24) NOT NULL,
 parameters_hash char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 quote_id char(36) CHARACTER SET ascii COLLATE ascii_bin NULL,
 state varchar(20) NOT NULL, business_type varchar(32), business_id bigint,
 result_json json, error_code varchar(80), confirmed_at datetime(6) NOT NULL,
 confirmation_audit_id char(36) CHARACTER SET ascii NOT NULL, control_epoch bigint NOT NULL,
 created_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6), completed_at datetime(6),
 KEY ix_operation_member(member_id,created_at),
 CONSTRAINT fk_operation_quote FOREIGN KEY(quote_id) REFERENCES oms_agent_quote(quote_id),
 CONSTRAINT ck_operation_state CHECK(state IN ('IN_TX','SUCCEEDED','REJECTED')),
 CONSTRAINT ck_operation_action CHECK(action_type IN ('ADD_CART','CREATE_ORDER','CANCEL_ORDER','CREATE_RETURN'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS oms_agent_cart_guard (
 member_id bigint NOT NULL PRIMARY KEY
) ENGINE=InnoDB COMMENT='Shared mutex for legacy and agent cart mutations and checkout';
