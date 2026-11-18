-- The public password below is for these two freshly created loopback demo accounts only.
SET @demo_password = '$2a$12$0bGHZ6Vmdh692M4e4y9lPe2dQLm1BrogWFOsAlVjMNYh.P5eRB6ge';
INSERT INTO ums_admin (id, username, password, nick_name, create_time, status)
VALUES (1, 'demo-admin', @demo_password, '演示管理员', NOW(), 1);
INSERT INTO ums_admin_role_relation (admin_id, role_id) VALUES (1, 5);
INSERT INTO ums_member (id, member_level_id, username, password, nickname, status, create_time, integration, growth)
VALUES (1, 1, 'demo-buyer', @demo_password, '演示买家', 1, NOW(), 10000, 10000);
INSERT INTO ums_member_receive_address (member_id, name, phone_number, default_status, post_code, province, city, region, detail_address)
VALUES (1, '演示收货人', '13800000000', 1, '100000', '北京市', '北京市', '东城区', '演示地址，仅供模拟发货');
INSERT INTO oms_company_address (address_name, send_status, receive_status, name, phone, province, city, region, detail_address)
VALUES ('演示仓库', 1, 1, '演示仓库', '13800000001', '北京市', '北京市', '东城区', '演示地址，仅供模拟退货');
UPDATE pms_sku_stock SET lock_stock = 0 WHERE lock_stock IS NOT NULL;

INSERT INTO ums_menu (id, parent_id, create_time, title, level, sort, name, icon, hidden) VALUES
(200, 0, NOW(), '智能客服', 0, 0, 'agent', 'ums-admin', 0),
(201, 200, NOW(), '客服工作台', 1, 5, 'agentService', NULL, 0),
(202, 200, NOW(), '知识管理', 1, 4, 'agentKnowledge', NULL, 0),
(203, 200, NOW(), '模型与提示词', 1, 3, 'agentModels', NULL, 0),
(204, 200, NOW(), '治理与任务', 1, 2, 'agentGovernance', NULL, 0),
(205, 200, NOW(), '业务操作', 1, 1, 'agentOperations', NULL, 0);
INSERT INTO ums_role_menu_relation (role_id, menu_id) SELECT 5, id FROM ums_menu WHERE id BETWEEN 200 AND 205;

INSERT INTO ums_resource_category (id, create_time, name, sort) VALUES (200, NOW(), '演示智能客服', 0);
INSERT INTO ums_resource (create_time, name, url, category_id) VALUES
(NOW(), '演示商品图片上传', '/minio/**', 200),
(NOW(), 'agent:action:read', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:action:reconcile', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:attachment:read', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:attachment:upload', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:audit:read', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:business:read', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:conversation:history:all', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:conversation:note', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:conversation:read', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:conversation:reply', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:events:subscribe', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:handoff:claim', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:handoff:transfer', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:job:read', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:job:retry', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:kb:edit', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:kb:publish', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:kb:read', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:logistics:read', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:logistics:write', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:model:activate', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:model:read', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:model:write', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:presence:write', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:prompt:publish', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:prompt:read', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:prompt:write', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:retention:execute', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:retention:preview', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:return:read', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:return:receive', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:return:refund', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:return:review', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:settings:read', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:settings:write', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:staff:manage', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:staff:read', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:statistics:read', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:usage:read', '/mall-agent/v1/admin/**', 200),
(NOW(), 'agent:usage:write', '/mall-agent/v1/admin/**', 200);
INSERT INTO ums_role_resource_relation (role_id, resource_id)
SELECT 5, id FROM ums_resource WHERE category_id = 200;
