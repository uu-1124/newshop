"""Architecture v0.2 contract additions. Design files only; no service connections."""
import json
import re


def extend_database(tables, mysql, post_extra):
    if any(t["name"] == "ag_event_clock" for t in tables):
        return tables, mysql, post_extra
    by_name = {t["name"]: t for t in tables}
    # Old conversation_version remains diagnostic optimistic state, not a run cancellation fence.
    for table_name in ["ag_run", "ag_action"]:
        by_name[table_name]["fields"] = [(n, typ, "对象乐观版本快照，仅供追踪；生成/确认失效使用control_epoch" if n == "conversation_version" else desc)
                                       for n, typ, desc in by_name[table_name]["fields"]]
    by_name["ag_job"]["fields"] = [(n, typ.replace("'KB_PARSE','KB_INDEX'", "'KB_PARSE','KB_INDEX','MODEL_COMPATIBILITY'"), desc)
                                   for n, typ, desc in by_name["ag_job"]["fields"]]

    def field(table, name, sql_type, description):
        by_name[table]["fields"].insert(-2, (name, sql_type, description))

    def add(name, title, requirements, fields, constraints=(), indexes=(), mutable=True):
        fields = list(fields) + [("created_at", "timestamptz NOT NULL DEFAULT now()", "创建时间，统一 UTC 存储")]
        if mutable:
            fields += [("updated_at", "timestamptz NOT NULL DEFAULT now()", "同事务维护更新时间"),
                       ("version", "bigint NOT NULL DEFAULT 1 CHECK (version > 0)", "乐观锁版本")]
        tables.append(dict(name=name, title=title, requirements=requirements, fields=fields,
                           constraints=list(constraints), indexes=list(indexes)))

    field("ag_conversation", "control_epoch", "bigint NOT NULL DEFAULT 1 CHECK (control_epoch > 0)",
          "接管、关闭、删除等控制变化递增；普通改名、已读不使生成失效")
    for table in ["ag_run", "ag_action"]:
        field(table, "control_epoch", "bigint NOT NULL CHECK (control_epoch > 0)", "生成或准备时的控制世代")
    field("ag_run", "config_snapshot", "jsonb NOT NULL DEFAULT '{}'::jsonb", "模型与提示词版本、图版本、运行限额；不含秘密")
    field("ag_job", "lease_owner", "varchar(100)", "任务执行者；写进度时必须匹配")
    field("ag_job", "fencing_token", "bigint NOT NULL DEFAULT 1 CHECK (fencing_token > 0)", "租约重新领取时递增，阻止旧执行者覆盖结果")
    field("ag_job", "next_attempt_at", "timestamptz NOT NULL DEFAULT now()", "首次领取与延时重试的最早时间；扫描不能提前执行")
    by_name["ag_job"]["indexes"] = ["CREATE INDEX ix_job_queue ON ag_job(next_attempt_at,id) WHERE state='QUEUED';"]
    field("ag_kb_version", "embedding_profile_version", "bigint", "与 embedding_profile_id 成对保存的不可变配置版本")
    event_table = by_name["ag_event_outbox"]
    event_table["fields"][0] = ("event_id", "bigint PRIMARY KEY CHECK (event_id > 0)",
                                "在事务末尾锁 ag_event_clock 后显式分配，持锁到提交；禁止 nextval")
    event_table["fields"].insert(3, ("control_epoch", "bigint", "控制世代；与 run_id 一起屏蔽过时生成"))
    add("ag_event_clock", "提交有序事件时钟", ["R03", "R06", "R18"], [
        ("singleton_id", "smallint PRIMARY KEY CHECK (singleton_id=1)", "全库单行事件时钟"),
        ("last_event_id", "bigint NOT NULL DEFAULT 0 CHECK (last_event_id >= 0)", "仅由事件写入器在事务末尾更新"),
        ("replay_floor", "bigint NOT NULL DEFAULT 0 CHECK (replay_floor >= 0 AND replay_floor <= last_event_id)",
         "已删除连续事件前缀上界；after 小于此值要求快照重同步")])
    add("ag_action_dispatch", "确认投递与结果核对", ["R08", "R09", "R11", "R12", "R18"], [
        ("action_id", "uuid PRIMARY KEY REFERENCES ag_action(id)", "一个业务操作只有一个投递记录"),
        ("state", "varchar(24) NOT NULL DEFAULT 'READY' CHECK (state IN ('READY','DISPATCHING','RECONCILING','TERMINAL','MANUAL_REQUIRED'))", "首次投递及仅核对状态"),
        ("confirmation_audit_id", "uuid NOT NULL REFERENCES ag_audit_log(id)", "实际点击确认的审计记录"),
        ("reconcile_grant_ciphertext", "bytea NOT NULL", "仅核对凭据的加密信封；不含会员登录令牌，不授予执行业务权"),
        ("grant_key_version", "varchar(80) NOT NULL", "库外信封加密密钥的版本"),
        ("grant_expires_at", "timestamptz NOT NULL", "过期重新认证后续签原操作核对权"),
        ("dispatch_started_at", "timestamptz", "首次执行尝试时刻；恢复不再次投递执行业务请求"),
        ("next_attempt_at", "timestamptz NOT NULL DEFAULT now()", "核对调度时刻"),
        ("attempts", "integer NOT NULL DEFAULT 0 CHECK (attempts >= 0)", "核对次数"),
        ("lease_owner", "varchar(100)", "核对 worker"),
        ("lease_until", "timestamptz", "核对租约"),
        ("fencing_token", "bigint NOT NULL DEFAULT 1 CHECK (fencing_token > 0)", "旧 worker 禁止覆盖结果"),
        ("last_error_code", "varchar(80)", "脱敏失败码")],
        indexes=["CREATE INDEX ix_dispatch_due ON ag_action_dispatch(next_attempt_at,action_id) WHERE state IN ('READY','DISPATCHING','RECONCILING');"])
    add("ag_model_profile_version", "不可变模型配置版本", ["R07", "R16", "R18"], [
        ("profile_id", "uuid NOT NULL REFERENCES ag_model_profile(id)", "所属能力配置"),
        ("version_no", "bigint NOT NULL CHECK (version_no > 0)", "对应保存时 profile.version，不覆盖旧版本"),
        ("name", "varchar(100) NOT NULL", "保存时显示名称"),
        ("enabled", "boolean NOT NULL", "该版本启用状态"),
        ("is_active", "boolean NOT NULL", "该版本活动状态；切换产生新版本"),
        ("capability", "varchar(20) NOT NULL", "调用能力"),
        ("provider", "varchar(80) NOT NULL", "供应商标识"),
        ("model_name", "varchar(160) NOT NULL", "实际模型标识"),
        ("endpoint_ref", "varchar(160) NOT NULL", "不可变端点注册版本名"),
        ("secret_version_ref", "varchar(200) NOT NULL", "仅受限适配器可解析；普通读取不返回"),
        ("public_options", "jsonb NOT NULL", "白名单内非秘密参数快照"),
        ("snapshot_sha256", "char(64) NOT NULL CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$')", "公开快照规范化哈希"),
        ("compatibility_evidence", "jsonb NOT NULL DEFAULT '{}'::jsonb", "脱敏证据快照；激活时引用匹配配置哈希与适配器版本的成功任务，不覆盖旧证据"),
        ("created_by", "bigint NOT NULL", "配置保存责任人")],
        constraints=["PRIMARY KEY(profile_id,version_no)"], mutable=False)
    add("ag_quota_reservation", "供应商调用额度预留", ["R16", "R18"], [
        ("id", "uuid PRIMARY KEY", "预留记录标识"),
        ("call_id", "uuid NOT NULL", "在供应商调用之前生成的稳定编号"),
        ("bucket_key", "varchar(180) NOT NULL", "能力/配置/主体/UTC日；不因版本切换清零"),
        ("period_start", "timestamptz NOT NULL", "计量窗口起点"),
        ("unit_kind", "varchar(24) NOT NULL CHECK (unit_kind IN ('TOKEN','SECOND','CHARACTER','REQUEST'))", "不同单位禁止混加"),
        ("reserved_units", "numeric(20,6) NOT NULL CHECK (reserved_units >= 0)", "调用前保守预留量"),
        ("settled_units", "numeric(20,6) CHECK (settled_units >= 0)", "未报告时为空"),
        ("state", "varchar(16) NOT NULL DEFAULT 'RESERVED' CHECK (state IN ('RESERVED','SETTLED','RELEASED','UNKNOWN'))", "未知计费不能因租约到期自动释放"),
        ("profile_id", "uuid NOT NULL REFERENCES ag_model_profile(id)", "计费所属配置"),
        ("profile_version", "bigint NOT NULL", "不可变模型版本"),
        ("expires_at", "timestamptz NOT NULL", "异常核对期限；不等于自动返还额度")],
        constraints=["UNIQUE(call_id,bucket_key)", "FOREIGN KEY(profile_id,profile_version) REFERENCES ag_model_profile_version(profile_id,version_no)"],
        indexes=["CREATE INDEX ix_quota_bucket ON ag_quota_reservation(bucket_key,period_start,state);"])
    add("ag_media_request", "会员语音请求与调用归属", ["R14", "R16", "R17"], [
        ("id", "uuid PRIMARY KEY", "语音请求编号"),
        ("member_id", "bigint NOT NULL CHECK(member_id>0)", "Java会员身份"),
        ("kind", "varchar(8) NOT NULL CHECK(kind IN ('ASR','TTS'))", "转写或播报"),
        ("idempotency_key", "uuid NOT NULL", "会员逻辑请求键"),
        ("request_hash", "char(64) NOT NULL", "来源与请求参数哈希"),
        ("cache_key", "char(64)", "当前回答文本、段落、音色及模型版本缓存键"),
        ("attachment_id", "uuid REFERENCES ag_attachment(id)", "转写的本人录音"),
        ("message_id", "uuid REFERENCES ag_message(id)", "播报的本人可见回答"),
        ("state", "varchar(16) NOT NULL DEFAULT 'RUNNING' CHECK(state IN ('RUNNING','SUCCEEDED','FAILED','UNKNOWN'))", "中断不自动重调供应商"),
        ("lease_owner", "varchar(100) NOT NULL", "当前HTTP执行者"),
        ("fencing_token", "bigint NOT NULL DEFAULT 1", "请求执行栅栏"),
        ("lease_until", "timestamptz NOT NULL DEFAULT now()+interval '5 minutes'", "供应商调用授权期限"),
        ("result", "jsonb", "转写或私有播报引用，按期限清理"),
        ("error_code", "varchar(80)", "脱敏错误码"),
        ("retention_until", "timestamptz NOT NULL DEFAULT now()+interval '7 days'", "最多7天保留")],
        constraints=["UNIQUE(member_id,kind,idempotency_key)", "CHECK((kind='ASR' AND attachment_id IS NOT NULL AND message_id IS NULL) OR (kind='TTS' AND message_id IS NOT NULL AND attachment_id IS NULL))"],
        indexes=["CREATE INDEX ix_media_retention ON ag_media_request(retention_until);"], mutable=False)
    field("ag_model_call", "media_request_id", "uuid", "关联会员语音请求，与run/job互斥")
    post_extra += """
ALTER TABLE ag_model_call ADD CONSTRAINT fk_call_media_request FOREIGN KEY(media_request_id) REFERENCES ag_media_request(id);
ALTER TABLE ag_model_call ADD CONSTRAINT ck_model_one_source CHECK(num_nonnulls(run_id,job_id,media_request_id)<=1);
ALTER TABLE ag_model_call ADD CONSTRAINT fk_call_model_version
  FOREIGN KEY(profile_id,profile_version) REFERENCES ag_model_profile_version(profile_id,version_no);
ALTER TABLE ag_kb_version ADD CONSTRAINT fk_kb_model_version
  FOREIGN KEY(embedding_profile_id,embedding_profile_version) REFERENCES ag_model_profile_version(profile_id,version_no);
ALTER TABLE ag_kb_version ADD CONSTRAINT ck_kb_model_pair
  CHECK ((embedding_profile_id IS NULL) = (embedding_profile_version IS NULL));
INSERT INTO ag_event_clock(singleton_id) VALUES (1);
"""
    mysql += """

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
"""
    return tables, mysql.replace("设计草案 v0.1", "设计草案 v0.2"), post_extra


def extend_api(schemas, paths, operations, op, ref, obj, arr, integer, st, uuid, time, expected, event_variants):
    if "ReconcileOperationInput" in schemas:
        return
    control = {**expected, "description": "控制世代；接管/停止/关闭/删除递增，普通改名/已读不使旧run失效"}
    for name in ["Conversation", "Run", "Action"]:
        schemas[name]["properties"]["control_epoch"] = control.copy()
        schemas[name]["required"].append("control_epoch")
    snapshot = schemas["Snapshot"]
    snapshot["properties"].update({"recent_messages": arr(ref("Message"), 100),
                                    "history_cursor": {"anyOf": [st(2048), {"type": "null"}]},
                                    "handoff": {"anyOf": [ref("Handoff"), {"type": "null"}]},
                                    "read_position": ref("ReadPosition")})
    snapshot["required"] += ["recent_messages", "history_cursor", "handoff", "read_position"]
    snapshot["description"] = "同一REPEATABLE READ事务返回会话、最近20条可见消息(含生成中正文)、run/action/接待/已读与event_cursor。历史继续使用history_cursor；首次订阅after=event_cursor。"
    for name in ["ConversationPage", "HandoffPage", "JobPage"]:
        schemas[name]["properties"]["event_cursor"] = {"type": "string", "pattern": "^(0|[1-9][0-9]*)$", "description": "与本页对象在同一RR快照读取的全局事件高水位"}
        schemas[name]["required"].append("event_cursor")
    paths["/mall-agent/v1/member/actions/{action_id}/confirm"]["post"]["description"] = "只接收action版本，不接收替换业务参数。核验会员、action当前版本、会话control_epoch及状态、报价与期限；普通改名不使有效卡片失效。重复确认只返回原状态；模型与自然语言不调用此入口。"
    schemas["ExecuteOperationInput"]["description"] = "confirmation_receipt由受控确认服务签发，绑定会员、action/operation_no、control_epoch、参数哈希、有效期、confirmed_at和实际点击审计。conversation_version仅为对象版本快照；改名不使有效确认失效。"
    for event in event_variants:
        event["properties"]["control_epoch"] = {"anyOf": [control.copy(), {"type": "null"}]}
        event["required"].append("control_epoch")
    schemas["ModelProfileUpdate"]["properties"]["model_name"] = st(160, minLength=1)
    schemas["ModelProfileUpdate"]["required"].append("model_name")
    schemas["ModelProfileVersion"] = obj({
        "profile_id": uuid, "version_no": expected, "capability": st(20), "provider": st(80),
        "name": st(100), "enabled": {"type": "boolean"}, "is_active": {"type": "boolean"},
        "model_name": st(160), "endpoint_ref": st(160), "public_options": ref("ModelOptions"),
        "snapshot_sha256": st(64, pattern="^[0-9a-f]{64}$"), "compatibility_status": {"type": "string", "enum": ["UNVERIFIED", "PASSED", "FAILED"]},
        "created_by": {"anyOf": [{"type": "string", "pattern": "^[1-9][0-9]{0,18}$"}, {"type": "null"}]}, "created_at": time},
        ["profile_id", "version_no", "name", "enabled", "is_active", "capability", "provider", "model_name", "endpoint_ref", "public_options", "snapshot_sha256", "compatibility_status", "created_by", "created_at"],
        "不可变公开配置快照；不返回 secret_ref 或 secret_version_ref。")
    schemas["ModelProfileVersionPage"] = obj({"items": arr(ref("ModelProfileVersion")), "next_cursor": {"anyOf": [st(2048), {"type": "null"}]}, "has_more": {"type": "boolean"}}, ["items", "next_cursor", "has_more"])
    op("get", "/mall-agent/v1/admin/model-profiles/{profile_id}/versions", "listModelProfileVersions",
       "查询不可变模型配置版本", "ModelProfileVersionPage", ["R16"], ["A03"], perm="agent:model:read",
       params=[{"name": "cursor", "in": "query", "required": False, "schema": st(2048)},
               {"name": "limit", "in": "query", "required": False, "schema": integer(1, 100)}])
    schemas["ReconcileOperationInput"] = obj({
        "mode": {"type": "string", "enum": ["QUERY_ONLY", "FINALIZE_IF_ABSENT"]},
        "parameters_hash": st(64, pattern="^[0-9a-f]{64}$")}, ["mode", "parameters_hash"],
        "只能查询原结果或封闭尚未提交的原编号。禁止执行、重试执行、改变业务参数。")
    path = "/mall-portal/agent-bridge/v1/operations/{operation_id}/reconcile"
    op("post", path, "reconcileBridgeOperation", "服务端核对或封闭未提交的原操作", "BusinessResult",
       ["R18"], ["B03", "B04", "B05"], "ReconcileOperationInput",
       description="仅后台核对服务可调用。BridgeSignature 与绑定 operation_no/member/action_type/parameters_hash 的 OperationReceipt 同时校验；无需持久化会员令牌。FINALIZE_IF_ABSENT 竞争同一 MySQL 操作主键锁：已提交返回原结果，确无提交则同事务写 REJECTED/RECOVERY_NOT_EXECUTED，阻止迟到执行。锁超时仍返回503，不能宣称失败。")
    paths[path]["post"]["security"] = [{"BridgeSignature": [], "OperationReceipt": []}]
    paths[path]["post"]["x-internal-reconciliation-only"] = True
    for operation in operations:
        if operation["id"] == "reconcileBridgeOperation":
            operation["internal_reconciliation_only"] = True


def extend_documents(root):
    """Keep regenerated prose consistent with architecture additions."""
    tables = json.loads((root / "database/table-catalog.json").read_text(encoding="utf-8"))
    ops = json.loads((root / "api/operation-catalog.json").read_text(encoding="utf-8"))
    for name in ["00-设计导航.md", "01-前端UI设计.md", "02-数据库设计.md", "03-接口设计.md", "04-需求设计映射.md", "05-设计决策与环境.md"]:
        path = root / name
        text = path.read_text(encoding="utf-8").replace("设计 v0.1", "设计 v0.2").replace("设计版本 v0.1", "设计版本 v0.2")
        text = text.replace("25 张 PostgreSQL", f"{len(tables)} 张 PostgreSQL").replace("7 张新增表", "9 张新增表")
        text = text.replace("共 114 个方法", f"共 {len(ops)} 个方法")
        text = text.replace("所有普通写接口 expected_version 不等返回 409", "所有要求版本的写接口 expected_version 不等返回 409")
        text = text.replace("每次事件先校验会话版本", "每次事件先校验控制世代control_epoch")
        text = text.replace("停止、人工接管、会话版本变更使旧 run", "停止、人工接管、control_epoch变更使旧 run")
        text = text.replace("offset 是目标文本的字符偏移", "offset 是目标文本的Unicode码点偏移")
        text = text.replace("桥接同时需要主体令牌和 X-Agent-Signature。", "普通桥接同时需要主体令牌和 X-Agent-Signature；仅核对例外见本文架构收口。")
        text = text.replace("原始路径与规范 query", "规范path+query（详细算法见架构03分册）")
        text = text.replace("锁会话并校验身份、版本和状态 → 检查消息 UUID/请求幂等", "先占请求幂等记录，再锁会话并校验身份、版本和状态 → 检查消息 UUID")
        text = text.replace("建议 delta 每 200ms 或 64 字合批入 outbox", "建议 delta 每 200ms 或 64 个Unicode码点合批入 outbox")
        text = text.replace("会话置 HUMAN_ACTIVE 并递增版本", "会话置 HUMAN_ACTIVE 并递增version与control_epoch")
        text = text.replace('"conversation_version":3,"occurred_at"', '"conversation_version":3,"control_epoch":2,"occurred_at"')
        text = text.replace("内部设计选择；未知结果只查原编号", "内部设计选择；未知结果仅核对原编号，可原子封闭未提交操作，禁止恢复执行")
        if name == "00-设计导航.md":
            text += "\n## 系统架构设计入口\n\n2026-11-11 完成架构设计 v0.2，需求基线保持 1.1。架构细化新增四张客服表、两张 Java 增量表和两个接口操作；均为设计文件，尚未迁移或实现。\n\n| 文件 | 内容 |\n|---|---|\n| 07-系统架构与模块设计.md | 总体架构、约束、关键决策与详细分册入口 |\n| architecture/01-模块与工程结构.md | 模块职责、工程分层、依赖与方法边界 |\n| architecture/02-LangGraph与工具编排.md | 图状态、节点、工具、模型、知识检索 |\n| architecture/03-关键流程与故障恢复.md | 时序、事务、状态机、事件与结果恢复 |\n| architecture/04-数据接口与页面承接.md | 全量需求、场景、接口、表及页面责任映射 |\n| architecture/05-部署运维与验收.md | 部署、配置、容量、清理、恢复及验收 |\n| 08-第一阶段开发与验收计划.md | 首阶段任务、依赖、完成门槛和后续阶段 |\n| architecture/coverage.json | 可机器核对的架构覆盖目录 |\n| architecture-validation.json | 本轮静态覆盖与契约检查结果 |\n"
        elif name == "01-前端UI设计.md":
            text += "\n## 架构收口 v0.2\n\nConversation/Run/Action 和事件新增 control_epoch，负责接管、关闭、删除后的旧生成屏蔽；version 保留乐观锁用途，改名和已读不应误停生成。模型编辑请求支持 model_name；历史版本通过 listModelProfileVersions 读取。移动端原型点击区域同步至少44px。原型仍不连接真实服务。\n"
        elif name == "02-数据库设计.md":
            text = text.replace("action 从待确认变 EXECUTING → 提交后调 Java", "action 从待确认变 EXECUTING，同事务写确认审计与 ag_action_dispatch → 提交后首次发送者调用一次 Java")
            text = text.replace("Python 调用超时标记 UNKNOWN，并仅用原 operation_no 查询", "Python 调用超时标记 UNKNOWN，后台使用原 operation_no 与仅核对凭据查询或原子封闭未提交操作")
            text = text.replace("持续显示核对中直到明确结果或人工查明", "查询未见结果时仍为 UNKNOWN；FINALIZE_IF_ABSENT 通过同一操作主键锁等待在途事务，或写入无业务副作用的 REJECTED 终态，锁超时继续核对")
            text = text.replace("事件 ID 为全局游标，存在间隙，重连无需连续编号", "事件 ID 通过事务末尾锁 ag_event_clock 显式分配并持锁到提交，禁止用 sequence/identity 代替提交顺序；时钟锁后不再取得业务锁")
            text += "\n## 架构收口 v0.2\n\n新增 ag_event_clock、ag_action_dispatch、ag_model_profile_version、ag_quota_reservation；新增 control_epoch、run.config_snapshot、任务租约栅栏、知识 embedding_profile_version 和组合外键。ag_model_profile_version 为不可变记录，调用与向量构建绑定确切版本。MySQL 新增 oms_agent_request_dedupe 与 oms_agent_business_outbox，分别保障员工写幂等和业务通知持久化。原来未执行的 v1 文件作为开发前目标定义更新，文件名保留；已有数据库后必须另建版本化迁移，不能整份重复执行。详细锁序、字段语义和恢复协议见架构03分册。\n"
        elif name == "03-接口设计.md":
            text = text.replace("id 为全局单调字符串，允许间隙", "id 为提交有序的全局单调字符串，允许间隙；由事务锁保护的事件时钟分配")
            text = text.replace("每次事件先校验会话版本", "每次事件先校验控制世代")
            text += "\n## 架构收口 v0.2\n\n普通22个Java桥接继续同时要求主体令牌与服务签名。新增 reconcileBridgeOperation 是唯一的后台核对专用例外，必须同时校验 BridgeSignature 和 OperationReceipt；不允许创建业务。QUERY_ONLY 查原结果；FINALIZE_IF_ABSENT 与执行竞争同一操作主键锁，已提交则返回结果，否则原子记录 REJECTED/RECOVERY_NOT_EXECUTED 阻止迟到执行。执行不由恢复 worker 重放；锁超时或依赖失败保持 UNKNOWN。\n\n新增 listModelProfileVersions 返回不可变脱敏配置版本。ModelProfileUpdate 支持 model_name，capability/provider 变更须新建配置。Conversation/Run/Action 及事件增加 control_epoch；version 用于乐观锁，control_epoch 用于生成隔离。上述变化均已同步 OpenAPI 与事件Schema。签名规范、仅核对凭据和跨语言哈希见 architecture/03-关键流程与故障恢复.md。\n"
            text += "\nSnapshot补充recent_messages/history_cursor/handoff/read_position，与event_cursor同一RR快照读取，避免先读游标再读到未来正文。ConversationPage/HandoffPage/JobPage补充同页event_cursor，员工首次订阅从各初始列表高水位的最小值补传，按对象version去重。\n"
        elif name == "04-需求设计映射.md":
            text += "\n## 架构与阶段追踪\n\narchitecture/04-数据接口与页面承接.md 逐项列出 R01–R18、U01–U12、A01–A15、C01–C08、H01–H07、全部接口、表和页面的模块责任及阶段。coverage.json 是同一目录的机器表示。第一阶段为阶段性交付，部分需求跨阶段完成，不把部分场景通过记为整条需求完成。\n"
        elif name == "05-设计决策与环境.md":
            text += "\n## 架构决策 v0.2\n\n| 编号 | 架构选择 | 约束 |\n|---|---|---|\n| D09 | 单Python工程，API、生成worker、通用worker、scheduler分进程 | 不拆成多个独立业务微服务；每个写模块有唯一责任 |\n| D10 | 事务末尾锁事件时钟分配提交有序event_id | 禁止sequence/identity的分配顺序冒充提交顺序 |\n| D11 | 确认与投递记录同事务，恢复只核对或原子封闭未提交操作 | 不持久化会员令牌，不恢复投递执行业务请求 |\n| D12 | 不可变模型配置版本与调用/知识构建绑定 | 秘密字节留在库外，读取接口不返回秘密引用 |\n| D13 | control_epoch区分控制变化与普通对象版本 | 领取、关闭、删除隔离旧生成；改名不误停 |\n| D14 | 调用前持久预留额度，未知计费保守占用 | 不将估算成本当供应商最终账单 |\n| D15 | Java员工写幂等与业务通知outbox独立持久化 | 物流节点、人工售后及延迟取消恢复均可核对 |\n\nD09–D15 是本轮实施架构选择，未扩大功能范围。D03/D04仍为业务口径建议，开发业务写入前用边界样例固定。2026-11-11前次只读采样确认7容器运行，约11GiB可用内存/30GiB剩余磁盘；该采样不替代部署时容量实测。官方OpenAI文档访问返回403，本轮没有据此认定模型服务已可调用。\n"
        path.write_text(text, encoding="utf-8")
