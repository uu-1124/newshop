-- Immutable demo rule versions. Original returns keep their recorded version string.
CREATE TABLE IF NOT EXISTS oms_after_sale_rule_version (
 id bigint NOT NULL AUTO_INCREMENT PRIMARY KEY,
 rule_code varchar(80) NOT NULL, version_no int NOT NULL,
 rules_json json NOT NULL, effective_at datetime(6) NOT NULL,
 created_by bigint NOT NULL, created_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 UNIQUE KEY uq_after_sale_rule(rule_code,version_no),
 KEY ix_after_sale_effective(rule_code,effective_at,version_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO oms_after_sale_rule_version(rule_code,version_no,rules_json,effective_at,created_by)
 SELECT 'ITEM_NET_DEMO',1,JSON_OBJECT('return_window_hours',168,'allow_shipped',true,'amount_basis','PAID_ITEM_NET','is_simulated',true,'note','原168小时演示规则基线'), '2000-01-01 00:00:00',0
 WHERE NOT EXISTS(SELECT 1 FROM oms_after_sale_rule_version WHERE rule_code='ITEM_NET_DEMO' AND version_no=1);
