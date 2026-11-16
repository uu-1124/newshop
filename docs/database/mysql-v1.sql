-- 设计草案 v0.2，目标 MySQL 8.0.35；本文件未执行。
-- 通过迁移器对已有 mall 库增量执行，不导入官方初始化 SQL。
-- DDL 隐式提交；先在数据库副本验证，迁移版本防重复，禁止无备份直接运行。
-- DATETIME(6) 统一按 UTC 写入，连接 time_zone='+00:00'；API 转换 RFC3339。
-- 不自动回填历史售后明细，不修改历史售后状态。
SET NAMES utf8mb4;

CREATE TABLE oms_agent_quote (
  quote_id char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  member_id bigint NOT NULL,
  action_type varchar(24) NOT NULL,
  parameters_hash char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  parameters_json json NOT NULL,
  pricing_snapshot json NOT NULL,
  checkout_fingerprint char(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  total_amount decimal(18,2) NOT NULL,
  currency char(3) NOT NULL DEFAULT 'CNY',
  rule_version varchar(80) NOT NULL,
  expires_at datetime(6) NOT NULL,
  created_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY(quote_id),
  KEY ix_quote_member(member_id,created_at),
  CONSTRAINT ck_quote_amount CHECK(total_amount>=0),
  CONSTRAINT ck_quote_action CHECK(action_type IN ('ADD_CART','CREATE_ORDER','CANCEL_ORDER','CREATE_RETURN'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Java 可靠报价及准备快照';

CREATE TABLE oms_agent_operation (
  operation_no char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  member_id bigint NOT NULL,
  action_type varchar(24) NOT NULL,
  parameters_hash char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  quote_id char(36) CHARACTER SET ascii COLLATE ascii_bin NULL,
  state varchar(20) NOT NULL,
  business_type varchar(32) NULL,
  business_id bigint NULL,
  result_json json NULL,
  error_code varchar(80) NULL,
  confirmed_at datetime(6) NOT NULL,
  created_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  completed_at datetime(6) NULL,
  PRIMARY KEY(operation_no),
  KEY ix_operation_member(member_id,created_at),
  CONSTRAINT fk_operation_quote FOREIGN KEY(quote_id) REFERENCES oms_agent_quote(quote_id),
  CONSTRAINT ck_operation_state CHECK(state IN ('IN_TX','SUCCEEDED','REJECTED')),
  CONSTRAINT ck_operation_action CHECK(action_type IN ('ADD_CART','CREATE_ORDER','CANCEL_ORDER','CREATE_RETURN'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='与业务写入同事务提交的幂等结果';

CREATE TABLE oms_after_sale_rule_version (
  id bigint NOT NULL AUTO_INCREMENT,
  rule_code varchar(80) NOT NULL,
  version_no int NOT NULL,
  rules_json json NOT NULL,
  effective_at datetime(6) NOT NULL,
  created_by bigint NOT NULL,
  created_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY(id),
  UNIQUE KEY uq_after_sale_rule(rule_code,version_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='售后规则不可变版本，旧申请保留原依据';

ALTER TABLE oms_order_return_apply
  ADD COLUMN order_item_id bigint NULL COMMENT '新增申请必须关联具体明细；历史保留空值',
  ADD COLUMN requested_amount decimal(18,2) NULL COMMENT '按实付分摊的申请金额',
  ADD COLUMN rule_version_id bigint NULL,
  ADD COLUMN eligibility_snapshot json NULL,
  ADD COLUMN process_stage varchar(32) NULL COMMENT '新流程阶段；历史空值按旧状态展示',
  ADD COLUMN row_version bigint NOT NULL DEFAULT 1,
  ADD KEY ix_return_item(order_item_id,status),
  ADD CONSTRAINT fk_return_order_item FOREIGN KEY(order_item_id) REFERENCES oms_order_item(id),
  ADD CONSTRAINT fk_return_rule_version FOREIGN KEY(rule_version_id) REFERENCES oms_after_sale_rule_version(id),
  ADD CONSTRAINT ck_return_requested_amount CHECK(requested_amount IS NULL OR requested_amount>=0),
  ADD CONSTRAINT ck_return_stage CHECK(process_stage IS NULL OR process_stage IN
    ('REQUESTED','APPROVED','GOODS_RECEIVED','SIMULATED_REFUNDED','REJECTED'));

CREATE TABLE oms_return_proof_reference (
  id bigint NOT NULL AUTO_INCREMENT,
  return_apply_id bigint NOT NULL,
  attachment_ref_id char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  attachment_id char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  sha256 char(64) NOT NULL,
  mime_type varchar(100) NOT NULL,
  byte_size bigint NOT NULL,
  created_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY(id),
  UNIQUE KEY uq_return_proof(return_apply_id,attachment_ref_id),
  CONSTRAINT fk_return_proof_apply FOREIGN KEY(return_apply_id) REFERENCES oms_order_return_apply(id),
  CONSTRAINT ck_return_proof_size CHECK(byte_size>0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='跨服务私有凭证引用，不存任意图片 URL';

CREATE TABLE oms_shipment (
  id bigint NOT NULL AUTO_INCREMENT,
  order_id bigint NOT NULL,
  carrier varchar(100) NULL,
  tracking_no varchar(100) NULL,
  is_simulated tinyint NOT NULL DEFAULT 1,
  row_version bigint NOT NULL DEFAULT 1,
  created_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY(id),
  UNIQUE KEY uq_shipment_order(order_id),
  CONSTRAINT fk_shipment_order FOREIGN KEY(order_id) REFERENCES oms_order(id),
  CONSTRAINT ck_shipment_demo CHECK(is_simulated=1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='当前每单一份模拟轨迹';

CREATE TABLE oms_shipment_event (
  id bigint NOT NULL AUTO_INCREMENT,
  shipment_id bigint NOT NULL,
  occurred_at datetime(6) NOT NULL,
  location varchar(200) NOT NULL,
  description varchar(1000) NOT NULL,
  is_simulated tinyint NOT NULL DEFAULT 1,
  created_by bigint NOT NULL,
  row_version bigint NOT NULL DEFAULT 1,
  created_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY(id),
  KEY ix_shipment_event_time(shipment_id,occurred_at,id),
  CONSTRAINT fk_shipment_event FOREIGN KEY(shipment_id) REFERENCES oms_shipment(id),
  CONSTRAINT ck_shipment_event_demo CHECK(is_simulated=1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='编辑节点不改变订单状态';

CREATE TABLE oms_agent_audit (
  id bigint NOT NULL AUTO_INCREMENT,
  request_id char(36) NOT NULL,
  operation_no char(36) NULL,
  actor_kind varchar(16) NOT NULL,
  actor_id bigint NULL,
  resource_type varchar(40) NOT NULL,
  resource_id varchar(100) NOT NULL,
  event_type varchar(80) NOT NULL,
  details_json json NOT NULL,
  created_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY(id),
  KEY ix_agent_audit_resource(resource_type,resource_id,created_at),
  KEY ix_agent_audit_request(request_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='物流、售后与业务执行审计；不存完整隐私或令牌';


CREATE TABLE oms_agent_request_dedupe (
  scope varchar(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  admin_id bigint NOT NULL,
  idempotency_key char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  request_hash char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  result_json json NOT NULL,
  created_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY(scope,admin_id,idempotency_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='员工物流售后写入与业务同事务保存的幂等结果';

CREATE TABLE oms_agent_business_outbox (
  id bigint NOT NULL AUTO_INCREMENT,
  event_key varchar(160) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  aggregate_type varchar(40) NOT NULL,
  aggregate_id bigint NOT NULL,
  event_type varchar(80) NOT NULL,
  payload_json json NOT NULL,
  available_at datetime(6) NOT NULL,
  published_at datetime(6) NULL,
  attempts int NOT NULL DEFAULT 0,
  created_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY(id), UNIQUE KEY uq_business_event(event_key),
  KEY ix_business_outbox(published_at,available_at,id),
  CONSTRAINT ck_business_attempts CHECK(attempts>=0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='延迟取消等通知与订单同事务登记，消费者单独幂等';
