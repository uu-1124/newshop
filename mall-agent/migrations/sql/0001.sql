-- 设计草案 v0.2，PostgreSQL 16；尚未执行。

-- 目标：独立 mall_agent 数据库的 public schema。

-- UUID 由服务生成，不依赖扩展；所有日期为 timestamptz。

BEGIN;

CREATE TABLE ag_staff_profile (
  admin_id bigint PRIMARY KEY CHECK (admin_id > 0),
  display_name varchar(80) NOT NULL,
  presence varchar(32) NOT NULL DEFAULT 'OFFLINE' CHECK (presence IN ('ONLINE','BUSY','OFFLINE')),
  capacity smallint NOT NULL DEFAULT 3 CHECK (capacity BETWEEN 1 AND 20),
  heartbeat_at timestamptz,
  presence_expires_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0)
);

COMMENT ON TABLE ag_staff_profile IS '客服接待资料';

COMMENT ON COLUMN ag_staff_profile.admin_id IS 'Java 员工 ID，不存登录密码';

COMMENT ON COLUMN ag_staff_profile.display_name IS '买家可见接待名称';

COMMENT ON COLUMN ag_staff_profile.presence IS '枚举：ONLINE BUSY OFFLINE';

COMMENT ON COLUMN ag_staff_profile.capacity IS '并行接待容量；建议默认 3';

COMMENT ON COLUMN ag_staff_profile.heartbeat_at IS '最后心跳时间';

COMMENT ON COLUMN ag_staff_profile.presence_expires_at IS '心跳租约，超时展示离线，保留接待归属';

COMMENT ON COLUMN ag_staff_profile.created_at IS '创建时间，统一 UTC 存储';

COMMENT ON COLUMN ag_staff_profile.updated_at IS '更新时间，由应用在同一事务内维护';

COMMENT ON COLUMN ag_staff_profile.version IS '乐观锁版本，成功更新递增';

CREATE INDEX ix_staff_presence ON ag_staff_profile(presence, presence_expires_at);

CREATE TABLE ag_conversation (
  id uuid PRIMARY KEY,
  member_id bigint NOT NULL CHECK (member_id > 0),
  title varchar(100) NOT NULL DEFAULT '新会话',
  state varchar(32) NOT NULL DEFAULT 'AI_ACTIVE' CHECK (state IN ('AI_ACTIVE','WAITING_HUMAN','HUMAN_ACTIVE','CLOSED')),
  waiting_ai_enabled boolean NOT NULL DEFAULT false,
  assigned_admin_id bigint REFERENCES ag_staff_profile(admin_id),
  entry_context jsonb NOT NULL DEFAULT '{}'::jsonb,
  next_message_seq bigint NOT NULL DEFAULT 1 CHECK (next_message_seq > 0),
  memory_epoch bigint NOT NULL DEFAULT 1 CHECK (memory_epoch > 0),
  waiting_since timestamptz,
  closed_at timestamptz,
  deleted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  control_epoch bigint NOT NULL DEFAULT 1 CHECK (control_epoch > 0),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
  CHECK (state <> 'HUMAN_ACTIVE' OR assigned_admin_id IS NOT NULL),
  CHECK (state = 'WAITING_HUMAN' OR NOT waiting_ai_enabled)
);

COMMENT ON TABLE ag_conversation IS '买家会话';

COMMENT ON COLUMN ag_conversation.id IS '服务端生成 UUID v4';

COMMENT ON COLUMN ag_conversation.member_id IS 'Java 会员 ID；跨库不设物理外键';

COMMENT ON COLUMN ag_conversation.title IS '用户可修改标题';

COMMENT ON COLUMN ag_conversation.state IS '枚举：AI_ACTIVE WAITING_HUMAN HUMAN_ACTIVE CLOSED';

COMMENT ON COLUMN ag_conversation.waiting_ai_enabled IS '等待人工期间用户是否选择继续 AI';

COMMENT ON COLUMN ag_conversation.assigned_admin_id IS '当前接待人';

COMMENT ON COLUMN ag_conversation.entry_context IS '仅入口类型和商品/订单 ID，不存未经核验的价格';

COMMENT ON COLUMN ag_conversation.next_message_seq IS '会话行锁内分配消息序号';

COMMENT ON COLUMN ag_conversation.memory_epoch IS '删除/清理时递增，使旧总结及检查点失效';

COMMENT ON COLUMN ag_conversation.waiting_since IS '排队时间';

COMMENT ON COLUMN ag_conversation.closed_at IS '关闭时间';

COMMENT ON COLUMN ag_conversation.deleted_at IS '买家逻辑删除后所有买家入口立即拒绝';

COMMENT ON COLUMN ag_conversation.created_at IS '创建时间，统一 UTC 存储';

COMMENT ON COLUMN ag_conversation.control_epoch IS '接管、关闭、删除等控制变化递增；普通改名、已读不使生成失效';

COMMENT ON COLUMN ag_conversation.updated_at IS '更新时间，由应用在同一事务内维护';

COMMENT ON COLUMN ag_conversation.version IS '乐观锁版本，成功更新递增';

CREATE INDEX ix_conversation_member ON ag_conversation(member_id, updated_at DESC, id DESC) WHERE deleted_at IS NULL;

CREATE INDEX ix_conversation_queue ON ag_conversation(waiting_since, id) WHERE state = 'WAITING_HUMAN' AND deleted_at IS NULL;

CREATE INDEX ix_conversation_staff ON ag_conversation(assigned_admin_id, updated_at DESC, id DESC) WHERE deleted_at IS NULL;

CREATE TABLE ag_message (
  id uuid PRIMARY KEY,
  conversation_id uuid NOT NULL REFERENCES ag_conversation(id),
  seq bigint NOT NULL CHECK (seq > 0),
  sender_kind varchar(32) NOT NULL DEFAULT 'MEMBER' CHECK (sender_kind IN ('MEMBER','ASSISTANT','STAFF','SYSTEM')),
  sender_id bigint NOT NULL DEFAULT 0 CHECK (sender_id >= 0),
  client_message_id uuid,
  status varchar(32) NOT NULL DEFAULT 'ACCEPTED' CHECK (status IN ('ACCEPTED','GENERATING','COMPLETED','STOPPED','FAILED','INTERRUPTED')),
  text_content text NOT NULL DEFAULT '',
  blocks jsonb NOT NULL DEFAULT '[]'::jsonb,
  run_id uuid,
  reply_to_message_id uuid REFERENCES ag_message(id),
  retention_until timestamptz NOT NULL,
  deleted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
  UNIQUE(conversation_id, seq),
  UNIQUE(conversation_id, client_message_id),
  CHECK ((sender_kind IN ('MEMBER','STAFF') AND sender_id > 0) OR (sender_kind IN ('ASSISTANT','SYSTEM') AND sender_id = 0))
);

COMMENT ON TABLE ag_message IS '对买家可见的消息';

COMMENT ON COLUMN ag_message.id IS '服务端生成 UUID v4';

COMMENT ON COLUMN ag_message.conversation_id IS '所属会话';

COMMENT ON COLUMN ag_message.seq IS '会话内递增序号；历史分页使用';

COMMENT ON COLUMN ag_message.sender_kind IS '枚举：MEMBER ASSISTANT STAFF SYSTEM';

COMMENT ON COLUMN ag_message.sender_id IS '人类主体 ID；系统与 AI 为 0';

COMMENT ON COLUMN ag_message.client_message_id IS '发送方提供的幂等 UUID；AI/系统消息可空';

COMMENT ON COLUMN ag_message.status IS '枚举：ACCEPTED GENERATING COMPLETED STOPPED FAILED INTERRUPTED';

COMMENT ON COLUMN ag_message.text_content IS '已过滤可展示文本；不存内部思考';

COMMENT ON COLUMN ag_message.blocks IS '按接口定义的商品、订单、来源及操作卡片';

COMMENT ON COLUMN ag_message.run_id IS '生成任务 ID，建表后补外键';

COMMENT ON COLUMN ag_message.reply_to_message_id IS '重试/回复关联；不得自动重放写操作';

COMMENT ON COLUMN ag_message.retention_until IS '默认消息时间加 90 天';

COMMENT ON COLUMN ag_message.deleted_at IS '删除后禁止读取与进入模型上下文';

COMMENT ON COLUMN ag_message.created_at IS '创建时间，统一 UTC 存储';

COMMENT ON COLUMN ag_message.updated_at IS '更新时间，由应用在同一事务内维护';

COMMENT ON COLUMN ag_message.version IS '乐观锁版本，成功更新递增';

CREATE INDEX ix_message_history ON ag_message(conversation_id, seq DESC) WHERE deleted_at IS NULL;

CREATE INDEX ix_message_expiry ON ag_message(retention_until) WHERE deleted_at IS NULL;

CREATE TABLE ag_staff_note (
  id uuid PRIMARY KEY,
  conversation_id uuid NOT NULL REFERENCES ag_conversation(id),
  admin_id bigint NOT NULL REFERENCES ag_staff_profile(admin_id),
  client_note_id uuid NOT NULL,
  body text NOT NULL CHECK (length(body) BETWEEN 1 AND 5000),
  retention_until timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(conversation_id, admin_id, client_note_id)
);

COMMENT ON TABLE ag_staff_note IS '内部备注';

COMMENT ON COLUMN ag_staff_note.id IS '服务端生成 UUID v4';

COMMENT ON COLUMN ag_staff_note.conversation_id IS '所属会话';

COMMENT ON COLUMN ag_staff_note.admin_id IS '备注作者';

COMMENT ON COLUMN ag_staff_note.client_note_id IS '员工发送幂等键';

COMMENT ON COLUMN ag_staff_note.body IS '仅有权员工可读，不进入买家事件或模型';

COMMENT ON COLUMN ag_staff_note.retention_until IS '备注留存策略';

COMMENT ON COLUMN ag_staff_note.created_at IS '创建时间，统一 UTC 存储';

CREATE TABLE ag_read_cursor (
  conversation_id uuid NOT NULL REFERENCES ag_conversation(id),
  actor_kind varchar(8) NOT NULL CHECK (actor_kind IN ('MEMBER','STAFF')),
  actor_id bigint NOT NULL CHECK (actor_id > 0),
  last_seq bigint NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
  PRIMARY KEY(conversation_id, actor_kind, actor_id)
);

COMMENT ON TABLE ag_read_cursor IS '已读位置';

COMMENT ON COLUMN ag_read_cursor.conversation_id IS '所属会话';

COMMENT ON COLUMN ag_read_cursor.actor_kind IS '主体类型';

COMMENT ON COLUMN ag_read_cursor.actor_id IS '从身份上下文取得';

COMMENT ON COLUMN ag_read_cursor.last_seq IS '仅允许向前移动，不超过可见最大序号';

COMMENT ON COLUMN ag_read_cursor.created_at IS '创建时间，统一 UTC 存储';

COMMENT ON COLUMN ag_read_cursor.updated_at IS '更新时间，由应用在同一事务内维护';

COMMENT ON COLUMN ag_read_cursor.version IS '乐观锁版本，成功更新递增';

CREATE TABLE ag_attachment (
  id uuid PRIMARY KEY,
  owner_kind varchar(8) NOT NULL CHECK (owner_kind IN ('MEMBER','STAFF')),
  owner_id bigint NOT NULL CHECK (owner_id > 0),
  purpose varchar(24) NOT NULL CHECK (purpose IN ('CHAT_IMAGE','KNOWLEDGE','RECORDING','SPEECH')),
  object_key varchar(512) NOT NULL UNIQUE,
  original_name varchar(255) NOT NULL,
  mime_type varchar(100) NOT NULL,
  byte_size bigint NOT NULL CHECK (byte_size > 0),
  sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  duration_ms integer CHECK (duration_ms BETWEEN 1 AND 60000),
  state varchar(32) NOT NULL DEFAULT 'UPLOADING' CHECK (state IN ('UPLOADING','READY','FAILED','DELETING','DELETED')),
  retention_until timestamptz,
  storage_deleted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0)
);

COMMENT ON TABLE ag_attachment IS '私有附件';

COMMENT ON COLUMN ag_attachment.id IS '服务端生成 UUID v4';

COMMENT ON COLUMN ag_attachment.owner_kind IS '上传主体类型';

COMMENT ON COLUMN ag_attachment.owner_id IS '上传主体 ID';

COMMENT ON COLUMN ag_attachment.purpose IS '用途决定限额和留存';

COMMENT ON COLUMN ag_attachment.object_key IS '私有桶内随机对象键，不接受任意 URL';

COMMENT ON COLUMN ag_attachment.original_name IS '展示文件名，不参与拼接存储路径';

COMMENT ON COLUMN ag_attachment.mime_type IS '实际字节检测所得 MIME';

COMMENT ON COLUMN ag_attachment.byte_size IS '实际字节数';

COMMENT ON COLUMN ag_attachment.sha256 IS '实际内容哈希';

COMMENT ON COLUMN ag_attachment.duration_ms IS '录音时长，TTS 限制另由配置控制';

COMMENT ON COLUMN ag_attachment.state IS '枚举：UPLOADING READY FAILED DELETING DELETED';

COMMENT ON COLUMN ag_attachment.retention_until IS '普通图片 90 天；音频 7 天；知识由引用决定';

COMMENT ON COLUMN ag_attachment.storage_deleted_at IS '对象物理删除时间';

COMMENT ON COLUMN ag_attachment.created_at IS '创建时间，统一 UTC 存储';

COMMENT ON COLUMN ag_attachment.updated_at IS '更新时间，由应用在同一事务内维护';

COMMENT ON COLUMN ag_attachment.version IS '乐观锁版本，成功更新递增';

CREATE INDEX ix_attachment_gc ON ag_attachment(retention_until, id) WHERE state = 'READY';

CREATE TABLE ag_run (
  id uuid PRIMARY KEY,
  conversation_id uuid NOT NULL REFERENCES ag_conversation(id),
  trigger_message_id uuid NOT NULL REFERENCES ag_message(id),
  attempt integer NOT NULL DEFAULT 1 CHECK (attempt > 0),
  retry_of_run_id uuid REFERENCES ag_run(id),
  state varchar(32) NOT NULL DEFAULT 'QUEUED' CHECK (state IN ('QUEUED','RUNNING','COMPLETED','STOP_REQUESTED','STOPPED','FAILED','INTERRUPTED')),
  conversation_version bigint NOT NULL,
  memory_epoch bigint NOT NULL,
  checkpoint_thread_id varchar(128) NOT NULL,
  lease_owner varchar(100),
  lease_until timestamptz,
  fencing_token bigint NOT NULL DEFAULT 1,
  started_at timestamptz,
  finished_at timestamptz,
  error_code varchar(80),
  created_at timestamptz NOT NULL DEFAULT now(),
  control_epoch bigint NOT NULL CHECK (control_epoch > 0),
  config_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
  UNIQUE(trigger_message_id, attempt)
);

COMMENT ON TABLE ag_run IS '独立生成任务';

COMMENT ON COLUMN ag_run.id IS '服务端生成 UUID v4';

COMMENT ON COLUMN ag_run.conversation_id IS '所属会话';

COMMENT ON COLUMN ag_run.trigger_message_id IS '用户消息；重试沿用原消息';

COMMENT ON COLUMN ag_run.attempt IS '每条触发消息第几次生成';

COMMENT ON COLUMN ag_run.retry_of_run_id IS '上一失败/停止的生成';

COMMENT ON COLUMN ag_run.state IS '枚举：QUEUED RUNNING COMPLETED STOP_REQUESTED STOPPED FAILED INTERRUPTED';

COMMENT ON COLUMN ag_run.conversation_version IS '对象乐观版本快照，仅供追踪；生成/确认失效使用control_epoch';

COMMENT ON COLUMN ag_run.memory_epoch IS '检查点与会话删除世代一致';

COMMENT ON COLUMN ag_run.checkpoint_thread_id IS '独立图 schema 的 thread_id，不存令牌';

COMMENT ON COLUMN ag_run.lease_owner IS '工作进程租约';

COMMENT ON COLUMN ag_run.lease_until IS '工作进程续约';

COMMENT ON COLUMN ag_run.fencing_token IS '旧工作进程不得续写';

COMMENT ON COLUMN ag_run.started_at IS '开始执行时间';

COMMENT ON COLUMN ag_run.finished_at IS '终止时间';

COMMENT ON COLUMN ag_run.error_code IS '对外稳定错误码，不含供应商原始敏感响应';

COMMENT ON COLUMN ag_run.created_at IS '创建时间，统一 UTC 存储';

COMMENT ON COLUMN ag_run.control_epoch IS '生成或准备时的控制世代';

COMMENT ON COLUMN ag_run.config_snapshot IS '模型与提示词版本、图版本、运行限额；不含秘密';

COMMENT ON COLUMN ag_run.updated_at IS '更新时间，由应用在同一事务内维护';

COMMENT ON COLUMN ag_run.version IS '乐观锁版本，成功更新递增';

CREATE UNIQUE INDEX uq_run_active_conversation ON ag_run(conversation_id) WHERE state IN ('QUEUED','RUNNING','STOP_REQUESTED');

CREATE INDEX ix_run_queue ON ag_run(created_at, id) WHERE state = 'QUEUED';

CREATE TABLE ag_generation_slot (
  slot_no smallint PRIMARY KEY CHECK (slot_no BETWEEN 1 AND 3),
  run_id uuid UNIQUE REFERENCES ag_run(id),
  lease_until timestamptz,
  fencing_token bigint NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0)
);

COMMENT ON TABLE ag_generation_slot IS '全局生成名额';

COMMENT ON COLUMN ag_generation_slot.slot_no IS '固定三个名额';

COMMENT ON COLUMN ag_generation_slot.run_id IS '被占用的任务';

COMMENT ON COLUMN ag_generation_slot.lease_until IS '异常时等待请求超时及清算后回收';

COMMENT ON COLUMN ag_generation_slot.fencing_token IS '每次领取递增';

COMMENT ON COLUMN ag_generation_slot.created_at IS '创建时间，统一 UTC 存储';

COMMENT ON COLUMN ag_generation_slot.updated_at IS '更新时间，由应用在同一事务内维护';

COMMENT ON COLUMN ag_generation_slot.version IS '乐观锁版本，成功更新递增';

CREATE TABLE ag_action (
  id uuid PRIMARY KEY,
  conversation_id uuid NOT NULL REFERENCES ag_conversation(id),
  member_id bigint NOT NULL CHECK (member_id > 0),
  action_type varchar(24) NOT NULL CHECK (action_type IN ('ADD_CART','CREATE_ORDER','CANCEL_ORDER','CREATE_RETURN')),
  state varchar(32) NOT NULL DEFAULT 'PENDING_CONFIRMATION' CHECK (state IN ('PENDING_CONFIRMATION','EXECUTING','SUCCEEDED','FAILED','UNKNOWN','CANCELLED','EXPIRED','STALE')),
  conversation_version bigint NOT NULL,
  parameters jsonb NOT NULL,
  parameters_hash char(64) NOT NULL CHECK (parameters_hash ~ '^[0-9a-f]{64}$'),
  quote_id uuid,
  operation_no uuid NOT NULL UNIQUE,
  expires_at timestamptz NOT NULL,
  confirmed_at timestamptz,
  result jsonb,
  error_code varchar(80),
  reconcile_after timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  control_epoch bigint NOT NULL CHECK (control_epoch > 0),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0)
);

COMMENT ON TABLE ag_action IS '不可变业务确认操作';

COMMENT ON COLUMN ag_action.id IS '服务端生成 UUID v4';

COMMENT ON COLUMN ag_action.conversation_id IS '所属会话';

COMMENT ON COLUMN ag_action.member_id IS 'Java 会员 ID；跨库不设物理外键';

COMMENT ON COLUMN ag_action.action_type IS '四种受控写操作';

COMMENT ON COLUMN ag_action.state IS '枚举：PENDING_CONFIRMATION EXECUTING SUCCEEDED FAILED UNKNOWN CANCELLED EXPIRED STALE';

COMMENT ON COLUMN ag_action.conversation_version IS '对象乐观版本快照，仅供追踪；生成/确认失效使用control_epoch';

COMMENT ON COLUMN ag_action.parameters IS '已规范化不可变业务参数；不含令牌';

COMMENT ON COLUMN ag_action.parameters_hash IS '按共享规范化算法计算';

COMMENT ON COLUMN ag_action.quote_id IS 'Java 报价；加车/取消也可带准备快照';

COMMENT ON COLUMN ag_action.operation_no IS '等于 action_id；Java/MySQL 唯一操作编号';

COMMENT ON COLUMN ag_action.expires_at IS '默认准备后 5 分钟';

COMMENT ON COLUMN ag_action.confirmed_at IS '有效用户点击时间';

COMMENT ON COLUMN ag_action.result IS '业务结果与脱敏卡片，不包含原始令牌';

COMMENT ON COLUMN ag_action.error_code IS '已知失败/失效原因';

COMMENT ON COLUMN ag_action.reconcile_after IS 'UNKNOWN 使用原编号核对时间';

COMMENT ON COLUMN ag_action.created_at IS '创建时间，统一 UTC 存储';

COMMENT ON COLUMN ag_action.control_epoch IS '生成或准备时的控制世代';

COMMENT ON COLUMN ag_action.updated_at IS '更新时间，由应用在同一事务内维护';

COMMENT ON COLUMN ag_action.version IS '乐观锁版本，成功更新递增';

CREATE INDEX ix_action_reconcile ON ag_action(reconcile_after) WHERE state IN ('EXECUTING','UNKNOWN');

CREATE INDEX ix_action_conversation ON ag_action(conversation_id, created_at DESC);

CREATE TABLE ag_handoff (
  id uuid PRIMARY KEY,
  conversation_id uuid NOT NULL REFERENCES ag_conversation(id),
  member_id bigint NOT NULL CHECK (member_id > 0),
  state varchar(32) NOT NULL DEFAULT 'WAITING' CHECK (state IN ('WAITING','CLAIMED','WITHDRAWN','CLOSED')),
  claimed_admin_id bigint REFERENCES ag_staff_profile(admin_id),
  reason varchar(500),
  claimed_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0)
);

COMMENT ON TABLE ag_handoff IS '排队与接待请求';

COMMENT ON COLUMN ag_handoff.id IS '服务端生成 UUID v4';

COMMENT ON COLUMN ag_handoff.conversation_id IS '所属会话';

COMMENT ON COLUMN ag_handoff.member_id IS 'Java 会员 ID；跨库不设物理外键';

COMMENT ON COLUMN ag_handoff.state IS '枚举：WAITING CLAIMED WITHDRAWN CLOSED';

COMMENT ON COLUMN ag_handoff.claimed_admin_id IS '领取人；转交后跟随会话';

COMMENT ON COLUMN ag_handoff.reason IS '转人工原因';

COMMENT ON COLUMN ag_handoff.claimed_at IS '领取时间';

COMMENT ON COLUMN ag_handoff.finished_at IS '结束时间';

COMMENT ON COLUMN ag_handoff.created_at IS '创建时间，统一 UTC 存储';

COMMENT ON COLUMN ag_handoff.updated_at IS '更新时间，由应用在同一事务内维护';

COMMENT ON COLUMN ag_handoff.version IS '乐观锁版本，成功更新递增';

CREATE UNIQUE INDEX uq_handoff_active ON ag_handoff(conversation_id) WHERE state IN ('WAITING','CLAIMED');

CREATE INDEX ix_handoff_wait ON ag_handoff(created_at, id) WHERE state = 'WAITING';

CREATE TABLE ag_event_outbox (
  event_id bigint PRIMARY KEY CHECK (event_id > 0),
  conversation_id uuid REFERENCES ag_conversation(id),
  conversation_version bigint,
  control_epoch bigint,
  event_type varchar(80) NOT NULL,
  visibility varchar(24) NOT NULL CHECK (visibility IN ('CONVERSATION','STAFF_ONLY','PERMISSION')),
  required_permission varchar(100),
  payload jsonb NOT NULL,
  published_at timestamptz,
  publish_attempts integer NOT NULL DEFAULT 0 CHECK (publish_attempts >= 0),
  retention_until timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((visibility = 'PERMISSION' AND required_permission IS NOT NULL) OR (visibility <> 'PERMISSION' AND conversation_id IS NOT NULL))
);

COMMENT ON TABLE ag_event_outbox IS '持久事件与推送发件箱';

COMMENT ON COLUMN ag_event_outbox.event_id IS '在事务末尾锁 ag_event_clock 后显式分配，持锁到提交；禁止 nextval';

COMMENT ON COLUMN ag_event_outbox.conversation_id IS '会话事件必填；管理任务事件可空';

COMMENT ON COLUMN ag_event_outbox.conversation_version IS '会话版本；旧生成不可继续展示';

COMMENT ON COLUMN ag_event_outbox.control_epoch IS '控制世代；与 run_id 一起屏蔽过时生成';

COMMENT ON COLUMN ag_event_outbox.event_type IS 'SSE 类型，见事件契约';

COMMENT ON COLUMN ag_event_outbox.visibility IS '每次订阅/补传重新鉴权';

COMMENT ON COLUMN ag_event_outbox.required_permission IS '全局管理事件要求的权限';

COMMENT ON COLUMN ag_event_outbox.payload IS '只包含该受众可见内容；内部备注单独 STAFF_ONLY';

COMMENT ON COLUMN ag_event_outbox.published_at IS '向通知通道发布成功时间；补传不依赖 Redis';

COMMENT ON COLUMN ag_event_outbox.publish_attempts IS '有限重试与监控';

COMMENT ON COLUMN ag_event_outbox.retention_until IS '建议事件补传保留 7 天，消息仍按 90 天';

COMMENT ON COLUMN ag_event_outbox.created_at IS '创建时间，统一 UTC 存储';

CREATE INDEX ix_outbox_unpublished ON ag_event_outbox(event_id) WHERE published_at IS NULL;

CREATE INDEX ix_outbox_replay ON ag_event_outbox(conversation_id, event_id);

CREATE TABLE ag_model_profile (
  id uuid PRIMARY KEY,
  name varchar(100) NOT NULL,
  capability varchar(20) NOT NULL CHECK (capability IN ('DIALOG','EMBEDDING','RERANK','VISION','ASR','TTS')),
  provider varchar(80) NOT NULL,
  model_name varchar(160) NOT NULL,
  endpoint_ref varchar(160) NOT NULL,
  secret_ref varchar(200) NOT NULL,
  public_options jsonb NOT NULL DEFAULT '{}'::jsonb,
  enabled boolean NOT NULL DEFAULT false,
  is_active boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
  CHECK (NOT is_active OR enabled)
);

COMMENT ON TABLE ag_model_profile IS '按能力隔离的模型配置';

COMMENT ON COLUMN ag_model_profile.id IS '服务端生成 UUID v4';

COMMENT ON COLUMN ag_model_profile.name IS '配置显示名';

COMMENT ON COLUMN ag_model_profile.capability IS '模型能力';

COMMENT ON COLUMN ag_model_profile.provider IS '供应商标识';

COMMENT ON COLUMN ag_model_profile.model_name IS '候选模型名，以实际接入验收为准';

COMMENT ON COLUMN ag_model_profile.endpoint_ref IS '受控端点注册名，不让普通配置绕过出站白名单';

COMMENT ON COLUMN ag_model_profile.secret_ref IS '秘密管理中的引用，普通读取接口不返回';

COMMENT ON COLUMN ag_model_profile.public_options IS '允许字段白名单，禁止放 API 密钥';

COMMENT ON COLUMN ag_model_profile.enabled IS '实测通过后启用';

COMMENT ON COLUMN ag_model_profile.is_active IS '每种能力唯一活动配置';

COMMENT ON COLUMN ag_model_profile.created_at IS '创建时间，统一 UTC 存储';

COMMENT ON COLUMN ag_model_profile.updated_at IS '更新时间，由应用在同一事务内维护';

COMMENT ON COLUMN ag_model_profile.version IS '乐观锁版本，成功更新递增';

CREATE UNIQUE INDEX uq_model_active_capability ON ag_model_profile(capability) WHERE is_active;

CREATE TABLE ag_prompt_version (
  id uuid PRIMARY KEY,
  prompt_key varchar(80) NOT NULL,
  version_no integer NOT NULL CHECK (version_no > 0),
  content text NOT NULL,
  state varchar(32) NOT NULL DEFAULT 'DRAFT' CHECK (state IN ('DRAFT','PUBLISHED','RETIRED')),
  sha256 char(64) NOT NULL,
  created_by bigint NOT NULL,
  published_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
  UNIQUE(prompt_key,version_no)
);

COMMENT ON TABLE ag_prompt_version IS '提示词版本';

COMMENT ON COLUMN ag_prompt_version.id IS '服务端生成 UUID v4';

COMMENT ON COLUMN ag_prompt_version.prompt_key IS '节点/职责标识';

COMMENT ON COLUMN ag_prompt_version.version_no IS '独立版本序号';

COMMENT ON COLUMN ag_prompt_version.content IS '后台受权限保护；不向买家输出';

COMMENT ON COLUMN ag_prompt_version.state IS '枚举：DRAFT PUBLISHED RETIRED';

COMMENT ON COLUMN ag_prompt_version.sha256 IS '模板摘要';

COMMENT ON COLUMN ag_prompt_version.created_by IS 'Java 员工 ID';

COMMENT ON COLUMN ag_prompt_version.published_at IS '发布时间';

COMMENT ON COLUMN ag_prompt_version.created_at IS '创建时间，统一 UTC 存储';

COMMENT ON COLUMN ag_prompt_version.updated_at IS '更新时间，由应用在同一事务内维护';

COMMENT ON COLUMN ag_prompt_version.version IS '乐观锁版本，成功更新递增';

CREATE UNIQUE INDEX uq_prompt_published ON ag_prompt_version(prompt_key) WHERE state='PUBLISHED';

CREATE TABLE ag_kb_document (
  id uuid PRIMARY KEY,
  title varchar(200) NOT NULL,
  category varchar(80) NOT NULL,
  visibility varchar(16) NOT NULL CHECK (visibility IN ('MEMBER','STAFF')),
  scope jsonb NOT NULL DEFAULT '{}'::jsonb,
  enabled boolean NOT NULL DEFAULT true,
  active_version_id uuid,
  created_by bigint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0)
);

COMMENT ON TABLE ag_kb_document IS '知识逻辑文档';

COMMENT ON COLUMN ag_kb_document.id IS '服务端生成 UUID v4';

COMMENT ON COLUMN ag_kb_document.title IS '知识标题';

COMMENT ON COLUMN ag_kb_document.category IS '配送/售后/FAQ/商品说明';

COMMENT ON COLUMN ag_kb_document.visibility IS '检索前授权，回答前复核';

COMMENT ON COLUMN ag_kb_document.scope IS '适用分类/商品等范围，空对象表示全站';

COMMENT ON COLUMN ag_kb_document.enabled IS '停用后所有新回答立即排除';

COMMENT ON COLUMN ag_kb_document.active_version_id IS '活动版本，建表后补同文档复合外键';

COMMENT ON COLUMN ag_kb_document.created_by IS '维护员工 ID';

COMMENT ON COLUMN ag_kb_document.created_at IS '创建时间，统一 UTC 存储';

COMMENT ON COLUMN ag_kb_document.updated_at IS '更新时间，由应用在同一事务内维护';

COMMENT ON COLUMN ag_kb_document.version IS '乐观锁版本，成功更新递增';

CREATE TABLE ag_kb_version (
  id uuid PRIMARY KEY,
  document_id uuid NOT NULL REFERENCES ag_kb_document(id),
  version_no integer NOT NULL CHECK (version_no > 0),
  state varchar(32) NOT NULL DEFAULT 'DRAFT' CHECK (state IN ('DRAFT','PARSING','PREVIEW_READY','INDEXING','READY','FAILED','RETIRED')),
  source_attachment_id uuid REFERENCES ag_attachment(id),
  source_text text,
  parsed_text text,
  source_sha256 char(64) NOT NULL,
  embedding_profile_id uuid REFERENCES ag_model_profile(id),
  embedding_dimension integer CHECK (embedding_dimension > 0),
  index_collection varchar(160),
  chunk_count integer NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
  quality_report jsonb,
  published_at timestamptz,
  created_by bigint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  embedding_profile_version bigint,
  updated_at timestamptz NOT NULL DEFAULT now(),
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
  UNIQUE(document_id,version_no),
  UNIQUE(document_id,id)
);

COMMENT ON TABLE ag_kb_version IS '知识不可变版本';

COMMENT ON COLUMN ag_kb_version.id IS '服务端生成 UUID v4';

COMMENT ON COLUMN ag_kb_version.document_id IS '所属文档';

COMMENT ON COLUMN ag_kb_version.version_no IS '文档内版本';

COMMENT ON COLUMN ag_kb_version.state IS '枚举：DRAFT PARSING PREVIEW_READY INDEXING READY FAILED RETIRED';

COMMENT ON COLUMN ag_kb_version.source_attachment_id IS '原始 PDF/DOCX；纯文本可空';

COMMENT ON COLUMN ag_kb_version.source_text IS '原始文本或编辑版原文';

COMMENT ON COLUMN ag_kb_version.parsed_text IS '预览确认后的文本';

COMMENT ON COLUMN ag_kb_version.source_sha256 IS '内容哈希';

COMMENT ON COLUMN ag_kb_version.embedding_profile_id IS '向量配置版本关联';

COMMENT ON COLUMN ag_kb_version.embedding_dimension IS '候选 1024，以实际验收锁定';

COMMENT ON COLUMN ag_kb_version.index_collection IS '含模型/维度代号的 Milvus 暂存集合';

COMMENT ON COLUMN ag_kb_version.chunk_count IS '成功切片数';

COMMENT ON COLUMN ag_kb_version.quality_report IS '质检题集结果，发布前检查';

COMMENT ON COLUMN ag_kb_version.published_at IS '活动切换时间';

COMMENT ON COLUMN ag_kb_version.created_by IS '创建员工 ID';

COMMENT ON COLUMN ag_kb_version.created_at IS '创建时间，统一 UTC 存储';

COMMENT ON COLUMN ag_kb_version.embedding_profile_version IS '与 embedding_profile_id 成对保存的不可变配置版本';

COMMENT ON COLUMN ag_kb_version.updated_at IS '更新时间，由应用在同一事务内维护';

COMMENT ON COLUMN ag_kb_version.version IS '乐观锁版本，成功更新递增';

CREATE TABLE ag_kb_chunk (
  id uuid PRIMARY KEY,
  version_id uuid NOT NULL REFERENCES ag_kb_version(id),
  ordinal integer NOT NULL CHECK (ordinal >= 0),
  chunk_kind varchar(8) NOT NULL CHECK (chunk_kind IN ('PARENT','CHILD')),
  parent_chunk_id uuid,
  content text NOT NULL,
  section_path jsonb NOT NULL DEFAULT '[]'::jsonb,
  page_start integer CHECK (page_start > 0),
  page_end integer,
  vector_key varchar(100),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(version_id,ordinal),
  UNIQUE(version_id,id),
  FOREIGN KEY(version_id,parent_chunk_id) REFERENCES ag_kb_chunk(version_id,id) DEFERRABLE INITIALLY DEFERRED,
  CHECK ((chunk_kind='PARENT' AND parent_chunk_id IS NULL) OR (chunk_kind='CHILD' AND parent_chunk_id IS NOT NULL)),
  CHECK (page_end IS NULL OR (page_start IS NOT NULL AND page_end >= page_start))
);

COMMENT ON TABLE ag_kb_chunk IS '知识父子切片';

COMMENT ON COLUMN ag_kb_chunk.id IS '服务端生成 UUID v4';

COMMENT ON COLUMN ag_kb_chunk.version_id IS '所属知识版本';

COMMENT ON COLUMN ag_kb_chunk.ordinal IS '版本内序号';

COMMENT ON COLUMN ag_kb_chunk.chunk_kind IS '子块召回、父块供回答';

COMMENT ON COLUMN ag_kb_chunk.parent_chunk_id IS '同版本父切片；复合外键';

COMMENT ON COLUMN ag_kb_chunk.content IS '可重建向量的可靠文本';

COMMENT ON COLUMN ag_kb_chunk.section_path IS '章节路径';

COMMENT ON COLUMN ag_kb_chunk.page_start IS '源文档可验证页码；无页码则空';

COMMENT ON COLUMN ag_kb_chunk.page_end IS '源结束页码';

COMMENT ON COLUMN ag_kb_chunk.vector_key IS 'Milvus 子切片主键';

COMMENT ON COLUMN ag_kb_chunk.metadata IS '来源定位和范围';

COMMENT ON COLUMN ag_kb_chunk.created_at IS '创建时间，统一 UTC 存储';

CREATE TABLE ag_attachment_ref (
  id uuid PRIMARY KEY,
  attachment_id uuid NOT NULL REFERENCES ag_attachment(id),
  ref_kind varchar(20) NOT NULL CHECK (ref_kind IN ('MESSAGE','KB_VERSION','RETURN_PROOF','SPEECH')),
  message_id uuid REFERENCES ag_message(id),
  kb_version_id uuid REFERENCES ag_kb_version(id),
  action_id uuid REFERENCES ag_action(id),
  return_apply_id bigint,
  protected boolean NOT NULL DEFAULT false,
  released_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
  CHECK ((ref_kind IN ('MESSAGE','SPEECH') AND message_id IS NOT NULL AND kb_version_id IS NULL AND action_id IS NULL) OR (ref_kind='KB_VERSION' AND kb_version_id IS NOT NULL AND message_id IS NULL AND action_id IS NULL) OR (ref_kind='RETURN_PROOF' AND action_id IS NOT NULL AND message_id IS NULL AND kb_version_id IS NULL))
);

COMMENT ON TABLE ag_attachment_ref IS '附件引用与售后保护';

COMMENT ON COLUMN ag_attachment_ref.id IS '服务端生成 UUID v4';

COMMENT ON COLUMN ag_attachment_ref.attachment_id IS '附件';

COMMENT ON COLUMN ag_attachment_ref.ref_kind IS '引用类型';

COMMENT ON COLUMN ag_attachment_ref.message_id IS '消息或播报对应消息';

COMMENT ON COLUMN ag_attachment_ref.kb_version_id IS '知识版本';

COMMENT ON COLUMN ag_attachment_ref.action_id IS '售后准备/执行关联；保护 UNKNOWN';

COMMENT ON COLUMN ag_attachment_ref.return_apply_id IS 'Java 售后申请 ID，无跨库外键';

COMMENT ON COLUMN ag_attachment_ref.protected IS '活动售后或结果未知时禁止物理删除';

COMMENT ON COLUMN ag_attachment_ref.released_at IS '释放引用的时间';

COMMENT ON COLUMN ag_attachment_ref.created_at IS '创建时间，统一 UTC 存储';

COMMENT ON COLUMN ag_attachment_ref.updated_at IS '更新时间，由应用在同一事务内维护';

COMMENT ON COLUMN ag_attachment_ref.version IS '乐观锁版本，成功更新递增';

CREATE UNIQUE INDEX uq_attachment_message_ref ON ag_attachment_ref(attachment_id,message_id,ref_kind) WHERE released_at IS NULL AND message_id IS NOT NULL;

CREATE UNIQUE INDEX uq_attachment_kb_ref ON ag_attachment_ref(attachment_id,kb_version_id) WHERE released_at IS NULL AND kb_version_id IS NOT NULL;

CREATE UNIQUE INDEX uq_attachment_action_ref ON ag_attachment_ref(attachment_id,action_id) WHERE released_at IS NULL AND action_id IS NOT NULL;

CREATE INDEX ix_attachment_active_refs ON ag_attachment_ref(attachment_id) WHERE released_at IS NULL;

CREATE TABLE ag_job (
  id uuid PRIMARY KEY,
  job_type varchar(32) NOT NULL CHECK (job_type IN ('KB_PARSE','KB_INDEX','MODEL_COMPATIBILITY','RETENTION_PREVIEW','RETENTION_EXECUTE','BACKUP_VERIFY','RESTORE_VERIFY','ATTACHMENT_DELETE','ACTION_RECONCILE')),
  state varchar(32) NOT NULL DEFAULT 'QUEUED' CHECK (state IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED')),
  dedupe_key varchar(180) NOT NULL,
  parameters jsonb NOT NULL,
  result jsonb,
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
  progress smallint NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
  lease_until timestamptz,
  error_code varchar(80),
  error_message varchar(500),
  created_by bigint,
  created_at timestamptz NOT NULL DEFAULT now(),
  lease_owner varchar(100),
  fencing_token bigint NOT NULL DEFAULT 1 CHECK (fencing_token > 0),
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
  UNIQUE(job_type,dedupe_key)
);

COMMENT ON TABLE ag_job IS '后台任务与清理恢复记录';

COMMENT ON COLUMN ag_job.id IS '服务端生成 UUID v4';

COMMENT ON COLUMN ag_job.job_type IS '任务种类';

COMMENT ON COLUMN ag_job.state IS '枚举：QUEUED RUNNING SUCCEEDED FAILED CANCELLED';

COMMENT ON COLUMN ag_job.dedupe_key IS '同版本/同批次不重复执行';

COMMENT ON COLUMN ag_job.parameters IS '类型校验后的 ID 与配置版本，不存凭据';

COMMENT ON COLUMN ag_job.result IS '数量、游标、质检或恢复核对结果';

COMMENT ON COLUMN ag_job.attempts IS '有限重试';

COMMENT ON COLUMN ag_job.max_attempts IS '重试上限';

COMMENT ON COLUMN ag_job.progress IS '已处理进度';

COMMENT ON COLUMN ag_job.lease_until IS '工作进程租约';

COMMENT ON COLUMN ag_job.error_code IS '稳定失败码';

COMMENT ON COLUMN ag_job.error_message IS '已脱敏失败原因';

COMMENT ON COLUMN ag_job.created_by IS '发起员工 ID；系统任务可空';

COMMENT ON COLUMN ag_job.created_at IS '创建时间，统一 UTC 存储';

COMMENT ON COLUMN ag_job.lease_owner IS '任务执行者；写进度时必须匹配';

COMMENT ON COLUMN ag_job.fencing_token IS '租约重新领取时递增，阻止旧执行者覆盖结果';

COMMENT ON COLUMN ag_job.next_attempt_at IS '首次领取与延时重试的最早时间；扫描不能提前执行';

COMMENT ON COLUMN ag_job.updated_at IS '更新时间，由应用在同一事务内维护';

COMMENT ON COLUMN ag_job.version IS '乐观锁版本，成功更新递增';

CREATE INDEX ix_job_queue ON ag_job(next_attempt_at,id) WHERE state='QUEUED';

CREATE TABLE ag_model_call (
  id uuid PRIMARY KEY,
  run_id uuid REFERENCES ag_run(id),
  job_id uuid REFERENCES ag_job(id),
  profile_id uuid NOT NULL REFERENCES ag_model_profile(id),
  profile_version bigint NOT NULL,
  prompt_version_id uuid REFERENCES ag_prompt_version(id),
  provider_request_id varchar(160),
  capability varchar(20) NOT NULL,
  status varchar(16) NOT NULL CHECK (status IN ('SUCCEEDED','FAILED','CANCELLED','UNKNOWN')),
  input_tokens bigint CHECK (input_tokens >= 0),
  output_tokens bigint CHECK (output_tokens >= 0),
  billable_units numeric(20,6) CHECK (billable_units >= 0),
  estimated_cost numeric(20,6) CHECK (estimated_cost >= 0),
  latency_ms bigint NOT NULL CHECK (latency_ms >= 0),
  error_code varchar(80),
  created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE ag_model_call IS '调用用量与错误';

COMMENT ON COLUMN ag_model_call.id IS '服务端生成 UUID v4';

COMMENT ON COLUMN ag_model_call.run_id IS '对话生成可关联；独立媒体任务可空';

COMMENT ON COLUMN ag_model_call.job_id IS '知识/清理等任务关联';

COMMENT ON COLUMN ag_model_call.profile_id IS '配置';

COMMENT ON COLUMN ag_model_call.profile_version IS '调用时配置版本';

COMMENT ON COLUMN ag_model_call.prompt_version_id IS '使用的提示词版本';

COMMENT ON COLUMN ag_model_call.provider_request_id IS '供应商请求追踪号';

COMMENT ON COLUMN ag_model_call.capability IS '按能力统计';

COMMENT ON COLUMN ag_model_call.status IS '调用结果';

COMMENT ON COLUMN ag_model_call.input_tokens IS '未报告时为空，不伪记为 0';

COMMENT ON COLUMN ag_model_call.output_tokens IS '未报告时为空';

COMMENT ON COLUMN ag_model_call.billable_units IS '语音时长、字符或其他计费单位';

COMMENT ON COLUMN ag_model_call.estimated_cost IS '估算值；不是供应商最终账单';

COMMENT ON COLUMN ag_model_call.latency_ms IS '耗时';

COMMENT ON COLUMN ag_model_call.error_code IS '稳定错误码';

COMMENT ON COLUMN ag_model_call.created_at IS '创建时间，统一 UTC 存储';

CREATE INDEX ix_model_call_time ON ag_model_call(created_at DESC,capability);

CREATE TABLE ag_audit_log (
  id uuid PRIMARY KEY,
  actor_kind varchar(16) NOT NULL,
  actor_id bigint,
  operation varchar(100) NOT NULL,
  resource_type varchar(60) NOT NULL,
  resource_id varchar(120) NOT NULL,
  request_id uuid NOT NULL,
  outcome varchar(24) NOT NULL,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE ag_audit_log IS '受限操作审计';

COMMENT ON COLUMN ag_audit_log.id IS '服务端生成 UUID v4';

COMMENT ON COLUMN ag_audit_log.actor_kind IS 'MEMBER/STAFF/SYSTEM';

COMMENT ON COLUMN ag_audit_log.actor_id IS '责任主体；系统可空';

COMMENT ON COLUMN ag_audit_log.operation IS '领取/发布/查看受限资料/执行等';

COMMENT ON COLUMN ag_audit_log.resource_type IS '资源类型';

COMMENT ON COLUMN ag_audit_log.resource_id IS '资源主键';

COMMENT ON COLUMN ag_audit_log.request_id IS '贯穿 API、任务和 Java 的追踪号';

COMMENT ON COLUMN ag_audit_log.outcome IS '成功/拒绝/失败';

COMMENT ON COLUMN ag_audit_log.details IS '脱敏摘要与版本差异，禁止秘密和整段私聊';

COMMENT ON COLUMN ag_audit_log.created_at IS '创建时间，统一 UTC 存储';

CREATE INDEX ix_audit_resource ON ag_audit_log(resource_type,resource_id,created_at DESC);

CREATE INDEX ix_audit_request ON ag_audit_log(request_id);

CREATE TABLE ag_feedback (
  id uuid PRIMARY KEY,
  message_id uuid NOT NULL REFERENCES ag_message(id),
  member_id bigint NOT NULL CHECK (member_id > 0),
  vote smallint NOT NULL CHECK (vote IN (-1,1)),
  reason varchar(500),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
  UNIQUE(member_id,message_id)
);

COMMENT ON TABLE ag_feedback IS '回答反馈';

COMMENT ON COLUMN ag_feedback.id IS '服务端生成 UUID v4';

COMMENT ON COLUMN ag_feedback.message_id IS '只能反馈本人会话中的可见 AI 回答';

COMMENT ON COLUMN ag_feedback.member_id IS 'Java 会员 ID；跨库不设物理外键';

COMMENT ON COLUMN ag_feedback.vote IS '赞为 1，踩为 -1';

COMMENT ON COLUMN ag_feedback.reason IS '可选理由';

COMMENT ON COLUMN ag_feedback.created_at IS '创建时间，统一 UTC 存储';

COMMENT ON COLUMN ag_feedback.updated_at IS '更新时间，由应用在同一事务内维护';

COMMENT ON COLUMN ag_feedback.version IS '乐观锁版本，成功更新递增';

CREATE TABLE ag_setting (
  key varchar(100) PRIMARY KEY,
  value jsonb NOT NULL,
  updated_by bigint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0)
);

COMMENT ON TABLE ag_setting IS '版本化业务与运维配置';

COMMENT ON COLUMN ag_setting.key IS '只允许受控键；秘密禁止入此表';

COMMENT ON COLUMN ag_setting.value IS '用对应配置 schema 校验';

COMMENT ON COLUMN ag_setting.updated_by IS '员工 ID';

COMMENT ON COLUMN ag_setting.created_at IS '创建时间，统一 UTC 存储';

COMMENT ON COLUMN ag_setting.updated_at IS '更新时间，由应用在同一事务内维护';

COMMENT ON COLUMN ag_setting.version IS '乐观锁版本，成功更新递增';

CREATE TABLE ag_context_summary (
  id uuid PRIMARY KEY,
  conversation_id uuid NOT NULL REFERENCES ag_conversation(id),
  memory_epoch bigint NOT NULL,
  through_seq bigint NOT NULL CHECK (through_seq > 0),
  summary text NOT NULL,
  retention_until timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(conversation_id,memory_epoch,through_seq)
);

COMMENT ON TABLE ag_context_summary IS '可清理上下文总结';

COMMENT ON COLUMN ag_context_summary.id IS '服务端生成 UUID v4';

COMMENT ON COLUMN ag_context_summary.conversation_id IS '所属会话';

COMMENT ON COLUMN ag_context_summary.memory_epoch IS '旧 epoch 的总结禁止使用';

COMMENT ON COLUMN ag_context_summary.through_seq IS '覆盖至消息序号';

COMMENT ON COLUMN ag_context_summary.summary IS '不作为业务事实，随源消息删除失效';

COMMENT ON COLUMN ag_context_summary.retention_until IS '不长于最早源消息的保留期';

COMMENT ON COLUMN ag_context_summary.created_at IS '创建时间，统一 UTC 存储';

CREATE TABLE ag_request_dedupe (
  scope varchar(120) NOT NULL,
  actor_kind varchar(8) NOT NULL CHECK (actor_kind IN ('MEMBER','STAFF')),
  actor_id bigint NOT NULL,
  idempotency_key uuid NOT NULL,
  request_hash char(64) NOT NULL,
  response_ref jsonb NOT NULL,
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(scope,actor_kind,actor_id,idempotency_key)
);

COMMENT ON TABLE ag_request_dedupe IS '普通写接口幂等记录';

COMMENT ON COLUMN ag_request_dedupe.scope IS '接口操作作用域';

COMMENT ON COLUMN ag_request_dedupe.actor_kind IS '主体类型';

COMMENT ON COLUMN ag_request_dedupe.actor_id IS '当前身份';

COMMENT ON COLUMN ag_request_dedupe.idempotency_key IS '请求头 Idempotency-Key';

COMMENT ON COLUMN ag_request_dedupe.request_hash IS '同键不同请求返回 409';

COMMENT ON COLUMN ag_request_dedupe.response_ref IS '已提交结果引用，重放前重新鉴权';

COMMENT ON COLUMN ag_request_dedupe.expires_at IS '建议普通请求至少保留 24 小时；业务幂等由 MySQL 永久约束';

COMMENT ON COLUMN ag_request_dedupe.created_at IS '创建时间，统一 UTC 存储';

CREATE TABLE ag_erasure_tombstone (
  resource_type varchar(40) NOT NULL,
  resource_id varchar(120) NOT NULL,
  memory_epoch bigint,
  erased_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(resource_type,resource_id)
);

COMMENT ON TABLE ag_erasure_tombstone IS '删除标记';

COMMENT ON COLUMN ag_erasure_tombstone.resource_type IS 'CONVERSATION/MESSAGE/ATTACHMENT';

COMMENT ON COLUMN ag_erasure_tombstone.resource_id IS '最小必要不可恢复标识';

COMMENT ON COLUMN ag_erasure_tombstone.memory_epoch IS '会话删除世代';

COMMENT ON COLUMN ag_erasure_tombstone.erased_at IS '所有消费者与恢复流程必须检查';

COMMENT ON COLUMN ag_erasure_tombstone.created_at IS '创建时间，统一 UTC 存储';

CREATE TABLE ag_event_clock (
  singleton_id smallint PRIMARY KEY CHECK (singleton_id=1),
  last_event_id bigint NOT NULL DEFAULT 0 CHECK (last_event_id >= 0),
  replay_floor bigint NOT NULL DEFAULT 0 CHECK (replay_floor >= 0 AND replay_floor <= last_event_id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0)
);

COMMENT ON TABLE ag_event_clock IS '提交有序事件时钟';

COMMENT ON COLUMN ag_event_clock.singleton_id IS '全库单行事件时钟';

COMMENT ON COLUMN ag_event_clock.last_event_id IS '仅由事件写入器在事务末尾更新';

COMMENT ON COLUMN ag_event_clock.replay_floor IS '已删除连续事件前缀上界；after 小于此值要求快照重同步';

COMMENT ON COLUMN ag_event_clock.created_at IS '创建时间，统一 UTC 存储';

COMMENT ON COLUMN ag_event_clock.updated_at IS '同事务维护更新时间';

COMMENT ON COLUMN ag_event_clock.version IS '乐观锁版本';

CREATE TABLE ag_action_dispatch (
  action_id uuid PRIMARY KEY REFERENCES ag_action(id),
  state varchar(24) NOT NULL DEFAULT 'READY' CHECK (state IN ('READY','DISPATCHING','RECONCILING','TERMINAL','MANUAL_REQUIRED')),
  confirmation_audit_id uuid NOT NULL REFERENCES ag_audit_log(id),
  reconcile_grant_ciphertext bytea NOT NULL,
  grant_key_version varchar(80) NOT NULL,
  grant_expires_at timestamptz NOT NULL,
  dispatch_started_at timestamptz,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  lease_owner varchar(100),
  lease_until timestamptz,
  fencing_token bigint NOT NULL DEFAULT 1 CHECK (fencing_token > 0),
  last_error_code varchar(80),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0)
);

COMMENT ON TABLE ag_action_dispatch IS '确认投递与结果核对';

COMMENT ON COLUMN ag_action_dispatch.action_id IS '一个业务操作只有一个投递记录';

COMMENT ON COLUMN ag_action_dispatch.state IS '首次投递及仅核对状态';

COMMENT ON COLUMN ag_action_dispatch.confirmation_audit_id IS '实际点击确认的审计记录';

COMMENT ON COLUMN ag_action_dispatch.reconcile_grant_ciphertext IS '仅核对凭据的加密信封；不含会员登录令牌，不授予执行业务权';

COMMENT ON COLUMN ag_action_dispatch.grant_key_version IS '库外信封加密密钥的版本';

COMMENT ON COLUMN ag_action_dispatch.grant_expires_at IS '过期重新认证后续签原操作核对权';

COMMENT ON COLUMN ag_action_dispatch.dispatch_started_at IS '首次执行尝试时刻；恢复不再次投递执行业务请求';

COMMENT ON COLUMN ag_action_dispatch.next_attempt_at IS '核对调度时刻';

COMMENT ON COLUMN ag_action_dispatch.attempts IS '核对次数';

COMMENT ON COLUMN ag_action_dispatch.lease_owner IS '核对 worker';

COMMENT ON COLUMN ag_action_dispatch.lease_until IS '核对租约';

COMMENT ON COLUMN ag_action_dispatch.fencing_token IS '旧 worker 禁止覆盖结果';

COMMENT ON COLUMN ag_action_dispatch.last_error_code IS '脱敏失败码';

COMMENT ON COLUMN ag_action_dispatch.created_at IS '创建时间，统一 UTC 存储';

COMMENT ON COLUMN ag_action_dispatch.updated_at IS '同事务维护更新时间';

COMMENT ON COLUMN ag_action_dispatch.version IS '乐观锁版本';

CREATE INDEX ix_dispatch_due ON ag_action_dispatch(next_attempt_at,action_id) WHERE state IN ('READY','DISPATCHING','RECONCILING');

CREATE TABLE ag_model_profile_version (
  profile_id uuid NOT NULL REFERENCES ag_model_profile(id),
  version_no bigint NOT NULL CHECK (version_no > 0),
  name varchar(100) NOT NULL,
  enabled boolean NOT NULL,
  is_active boolean NOT NULL,
  capability varchar(20) NOT NULL,
  provider varchar(80) NOT NULL,
  model_name varchar(160) NOT NULL,
  endpoint_ref varchar(160) NOT NULL,
  secret_version_ref varchar(200) NOT NULL,
  public_options jsonb NOT NULL,
  snapshot_sha256 char(64) NOT NULL CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$'),
  compatibility_evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by bigint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(profile_id,version_no)
);

COMMENT ON TABLE ag_model_profile_version IS '不可变模型配置版本';

COMMENT ON COLUMN ag_model_profile_version.profile_id IS '所属能力配置';

COMMENT ON COLUMN ag_model_profile_version.version_no IS '对应保存时 profile.version，不覆盖旧版本';

COMMENT ON COLUMN ag_model_profile_version.name IS '保存时显示名称';

COMMENT ON COLUMN ag_model_profile_version.enabled IS '该版本启用状态';

COMMENT ON COLUMN ag_model_profile_version.is_active IS '该版本活动状态；切换产生新版本';

COMMENT ON COLUMN ag_model_profile_version.capability IS '调用能力';

COMMENT ON COLUMN ag_model_profile_version.provider IS '供应商标识';

COMMENT ON COLUMN ag_model_profile_version.model_name IS '实际模型标识';

COMMENT ON COLUMN ag_model_profile_version.endpoint_ref IS '不可变端点注册版本名';

COMMENT ON COLUMN ag_model_profile_version.secret_version_ref IS '仅受限适配器可解析；普通读取不返回';

COMMENT ON COLUMN ag_model_profile_version.public_options IS '白名单内非秘密参数快照';

COMMENT ON COLUMN ag_model_profile_version.snapshot_sha256 IS '公开快照规范化哈希';

COMMENT ON COLUMN ag_model_profile_version.compatibility_evidence IS '脱敏证据快照；激活时引用匹配配置哈希与适配器版本的成功任务，不覆盖旧证据';

COMMENT ON COLUMN ag_model_profile_version.created_by IS '配置保存责任人';

COMMENT ON COLUMN ag_model_profile_version.created_at IS '创建时间，统一 UTC 存储';

CREATE TABLE ag_quota_reservation (
  id uuid PRIMARY KEY,
  call_id uuid NOT NULL,
  bucket_key varchar(180) NOT NULL,
  period_start timestamptz NOT NULL,
  unit_kind varchar(24) NOT NULL CHECK (unit_kind IN ('TOKEN','SECOND','CHARACTER','REQUEST')),
  reserved_units numeric(20,6) NOT NULL CHECK (reserved_units >= 0),
  settled_units numeric(20,6) CHECK (settled_units >= 0),
  state varchar(16) NOT NULL DEFAULT 'RESERVED' CHECK (state IN ('RESERVED','SETTLED','RELEASED','UNKNOWN')),
  profile_id uuid NOT NULL REFERENCES ag_model_profile(id),
  profile_version bigint NOT NULL,
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
  UNIQUE(call_id,bucket_key),
  FOREIGN KEY(profile_id,profile_version) REFERENCES ag_model_profile_version(profile_id,version_no)
);

COMMENT ON TABLE ag_quota_reservation IS '供应商调用额度预留';

COMMENT ON COLUMN ag_quota_reservation.id IS '预留记录标识';

COMMENT ON COLUMN ag_quota_reservation.call_id IS '在供应商调用之前生成的稳定编号';

COMMENT ON COLUMN ag_quota_reservation.bucket_key IS '能力/配置/主体/UTC日；不因版本切换清零';

COMMENT ON COLUMN ag_quota_reservation.period_start IS '计量窗口起点';

COMMENT ON COLUMN ag_quota_reservation.unit_kind IS '不同单位禁止混加';

COMMENT ON COLUMN ag_quota_reservation.reserved_units IS '调用前保守预留量';

COMMENT ON COLUMN ag_quota_reservation.settled_units IS '未报告时为空';

COMMENT ON COLUMN ag_quota_reservation.state IS '未知计费不能因租约到期自动释放';

COMMENT ON COLUMN ag_quota_reservation.profile_id IS '计费所属配置';

COMMENT ON COLUMN ag_quota_reservation.profile_version IS '不可变模型版本';

COMMENT ON COLUMN ag_quota_reservation.expires_at IS '异常核对期限；不等于自动返还额度';

COMMENT ON COLUMN ag_quota_reservation.created_at IS '创建时间，统一 UTC 存储';

COMMENT ON COLUMN ag_quota_reservation.updated_at IS '同事务维护更新时间';

COMMENT ON COLUMN ag_quota_reservation.version IS '乐观锁版本';

CREATE INDEX ix_quota_bucket ON ag_quota_reservation(bucket_key,period_start,state);


ALTER TABLE ag_message ADD CONSTRAINT fk_message_run FOREIGN KEY(run_id) REFERENCES ag_run(id);
ALTER TABLE ag_kb_document ADD CONSTRAINT fk_kb_active_same_document
    FOREIGN KEY(id,active_version_id) REFERENCES ag_kb_version(document_id,id)
    DEFERRABLE INITIALLY DEFERRED;
INSERT INTO ag_generation_slot(slot_no) VALUES (1),(2),(3);
-- langgraph_checkpoint schema 由锁定版本的 AsyncPostgresSaver.setup 管理。
-- 不手写、替换或删除组件内部检查点表；连接 search_path 须明确限定。
CREATE SCHEMA langgraph_checkpoint;

ALTER TABLE ag_model_call ADD CONSTRAINT fk_call_model_version
  FOREIGN KEY(profile_id,profile_version) REFERENCES ag_model_profile_version(profile_id,version_no);
ALTER TABLE ag_kb_version ADD CONSTRAINT fk_kb_model_version
  FOREIGN KEY(embedding_profile_id,embedding_profile_version) REFERENCES ag_model_profile_version(profile_id,version_no);
ALTER TABLE ag_kb_version ADD CONSTRAINT ck_kb_model_pair
  CHECK ((embedding_profile_id IS NULL) = (embedding_profile_version IS NULL));
INSERT INTO ag_event_clock(singleton_id) VALUES (1);


COMMIT;