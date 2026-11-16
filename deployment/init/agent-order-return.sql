-- Incremental demo order, return and logistics extension; never resets existing business rows.
CREATE TABLE IF NOT EXISTS oms_agent_return (
 return_id bigint PRIMARY KEY, order_id bigint NOT NULL, order_item_id bigint NOT NULL,
 quantity int NOT NULL, requested_amount decimal(18,2) NOT NULL, approved_amount decimal(18,2),
 stage varchar(32) NOT NULL, rule_version varchar(80) NOT NULL, version bigint NOT NULL DEFAULT 1,
 proof_metadata json NOT NULL, company_address_snapshot json,
 simulated_refund_no char(36) CHARACTER SET ascii COLLATE ascii_bin,
 created_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6), updated_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 KEY ix_agent_return_item(order_item_id,stage), KEY ix_agent_return_order(order_id,return_id),
 UNIQUE KEY uq_demo_refund(simulated_refund_no),
 CONSTRAINT ck_agent_return_quantity CHECK(quantity BETWEEN 1 AND 999),
 CONSTRAINT ck_agent_return_money CHECK(requested_amount>=0 AND (approved_amount IS NULL OR (approved_amount>=0 AND approved_amount<=requested_amount))),
 CONSTRAINT ck_agent_return_stage CHECK(stage IN ('REQUESTED','APPROVED','GOODS_RECEIVED','SIMULATED_REFUNDED','REJECTED'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS oms_agent_shipment (
 id bigint AUTO_INCREMENT PRIMARY KEY, order_id bigint NOT NULL UNIQUE,
 carrier varchar(100) NOT NULL, tracking_no varchar(100) NOT NULL, version bigint NOT NULL DEFAULT 1,
 created_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6), updated_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS oms_agent_shipment_event (
 id bigint AUTO_INCREMENT PRIMARY KEY, shipment_id bigint NOT NULL,
 occurred_at datetime(6) NOT NULL, location varchar(200) NOT NULL, description varchar(1000) NOT NULL,
 version bigint NOT NULL DEFAULT 1, created_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 KEY ix_shipment_timeline(shipment_id,occurred_at,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS oms_agent_staff_command (
 staff_id bigint NOT NULL, command_key char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 command_hash char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, result_json json,
 created_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6), PRIMARY KEY(staff_id,command_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS oms_agent_business_audit (
 id bigint AUTO_INCREMENT PRIMARY KEY, actor_kind varchar(16) NOT NULL, actor_id bigint NOT NULL,
 operation varchar(80) NOT NULL, resource_id bigint NOT NULL, request_id char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 before_json json, after_json json, reason varchar(1000) NOT NULL,
 created_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6), KEY ix_business_audit_resource(resource_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
