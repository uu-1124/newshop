"""Generate reviewable DDL and its data dictionary. Never connects to a database."""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]
TABLES = []

def table(name, title, requirements, fields, constraints=(), indexes=(), mutable=True):
    fields = list(fields)
    if not any(f[0] == "created_at" for f in fields):
        fields.append(("created_at", "timestamptz NOT NULL DEFAULT now()", "创建时间，统一 UTC 存储"))
    if mutable:
        fields.extend([
            ("updated_at", "timestamptz NOT NULL DEFAULT now()", "更新时间，由应用在同一事务内维护"),
            ("version", "bigint NOT NULL DEFAULT 1 CHECK (version > 0)", "乐观锁版本，成功更新递增")])
    TABLES.append(dict(name=name, title=title, requirements=requirements, fields=fields,
                       constraints=list(constraints), indexes=list(indexes)))

def enum(values):
    return ",".join("'" + x + "'" for x in values.split())

def state(name, values, default):
    return (name, "varchar(32) NOT NULL DEFAULT '" + default + "' CHECK (" + name + " IN (" + enum(values) + "))", "枚举：" + values)

ID = ("id", "uuid PRIMARY KEY", "服务端生成 UUID v4")
CONV = ("conversation_id", "uuid NOT NULL REFERENCES ag_conversation(id)", "所属会话")
MEMBER = ("member_id", "bigint NOT NULL CHECK (member_id > 0)", "Java 会员 ID；跨库不设物理外键")

table("ag_staff_profile", "客服接待资料", ["R01","R06"], [
    ("admin_id","bigint PRIMARY KEY CHECK (admin_id > 0)","Java 员工 ID，不存登录密码"),
    ("display_name","varchar(80) NOT NULL","买家可见接待名称"),
    state("presence","ONLINE BUSY OFFLINE","OFFLINE"),
    ("capacity","smallint NOT NULL DEFAULT 3 CHECK (capacity BETWEEN 1 AND 20)","并行接待容量；建议默认 3"),
    ("heartbeat_at","timestamptz","最后心跳时间"),
    ("presence_expires_at","timestamptz","心跳租约，超时展示离线，保留接待归属")],
    indexes=["CREATE INDEX ix_staff_presence ON ag_staff_profile(presence, presence_expires_at);"])

table("ag_conversation","买家会话",["R01","R02","R06"],[
    ID, MEMBER, ("title","varchar(100) NOT NULL DEFAULT '新会话'","用户可修改标题"),
    state("state","AI_ACTIVE WAITING_HUMAN HUMAN_ACTIVE CLOSED","AI_ACTIVE"),
    ("waiting_ai_enabled","boolean NOT NULL DEFAULT false","等待人工期间用户是否选择继续 AI"),
    ("assigned_admin_id","bigint REFERENCES ag_staff_profile(admin_id)","当前接待人"),
    ("entry_context","jsonb NOT NULL DEFAULT '{}'::jsonb","仅入口类型和商品/订单 ID，不存未经核验的价格"),
    ("next_message_seq","bigint NOT NULL DEFAULT 1 CHECK (next_message_seq > 0)","会话行锁内分配消息序号"),
    ("memory_epoch","bigint NOT NULL DEFAULT 1 CHECK (memory_epoch > 0)","删除/清理时递增，使旧总结及检查点失效"),
    ("waiting_since","timestamptz","排队时间"),
    ("closed_at","timestamptz","关闭时间"),
    ("deleted_at","timestamptz","买家逻辑删除后所有买家入口立即拒绝")],
    constraints=["CHECK (state <> 'HUMAN_ACTIVE' OR assigned_admin_id IS NOT NULL)",
                 "CHECK (state = 'WAITING_HUMAN' OR NOT waiting_ai_enabled)"],
    indexes=["CREATE INDEX ix_conversation_member ON ag_conversation(member_id, updated_at DESC, id DESC) WHERE deleted_at IS NULL;",
             "CREATE INDEX ix_conversation_queue ON ag_conversation(waiting_since, id) WHERE state = 'WAITING_HUMAN' AND deleted_at IS NULL;",
             "CREATE INDEX ix_conversation_staff ON ag_conversation(assigned_admin_id, updated_at DESC, id DESC) WHERE deleted_at IS NULL;"])

table("ag_message","对买家可见的消息",["R02","R03","R06","R13","R14"],[
    ID, CONV, ("seq","bigint NOT NULL CHECK (seq > 0)","会话内递增序号；历史分页使用"),
    state("sender_kind","MEMBER ASSISTANT STAFF SYSTEM","MEMBER"),
    ("sender_id","bigint NOT NULL DEFAULT 0 CHECK (sender_id >= 0)","人类主体 ID；系统与 AI 为 0"),
    ("client_message_id","uuid","发送方提供的幂等 UUID；AI/系统消息可空"),
    state("status","ACCEPTED GENERATING COMPLETED STOPPED FAILED INTERRUPTED","ACCEPTED"),
    ("text_content","text NOT NULL DEFAULT ''","已过滤可展示文本；不存内部思考"),
    ("blocks","jsonb NOT NULL DEFAULT '[]'::jsonb","按接口定义的商品、订单、来源及操作卡片"),
    ("run_id","uuid","生成任务 ID，建表后补外键"),
    ("reply_to_message_id","uuid REFERENCES ag_message(id)","重试/回复关联；不得自动重放写操作"),
    ("retention_until","timestamptz NOT NULL","默认消息时间加 90 天"),
    ("deleted_at","timestamptz","删除后禁止读取与进入模型上下文")],
    constraints=["UNIQUE(conversation_id, seq)","UNIQUE(conversation_id, client_message_id)",
                 "CHECK ((sender_kind IN ('MEMBER','STAFF') AND sender_id > 0) OR (sender_kind IN ('ASSISTANT','SYSTEM') AND sender_id = 0))"],
    indexes=["CREATE INDEX ix_message_history ON ag_message(conversation_id, seq DESC) WHERE deleted_at IS NULL;",
             "CREATE INDEX ix_message_expiry ON ag_message(retention_until) WHERE deleted_at IS NULL;"])

table("ag_staff_note","内部备注",["R06"],[ID, CONV,
    ("admin_id","bigint NOT NULL REFERENCES ag_staff_profile(admin_id)","备注作者"),
    ("client_note_id","uuid NOT NULL","员工发送幂等键"),
    ("body","text NOT NULL CHECK (length(body) BETWEEN 1 AND 5000)","仅有权员工可读，不进入买家事件或模型"),
    ("retention_until","timestamptz NOT NULL","备注留存策略")],
    constraints=["UNIQUE(conversation_id, admin_id, client_note_id)"], mutable=False)

table("ag_read_cursor","已读位置",["R02","R06"],[
    ("conversation_id","uuid NOT NULL REFERENCES ag_conversation(id)","所属会话"),
    ("actor_kind","varchar(8) NOT NULL CHECK (actor_kind IN ('MEMBER','STAFF'))","主体类型"),
    ("actor_id","bigint NOT NULL CHECK (actor_id > 0)","从身份上下文取得"),
    ("last_seq","bigint NOT NULL DEFAULT 0 CHECK (last_seq >= 0)","仅允许向前移动，不超过可见最大序号")],
    constraints=["PRIMARY KEY(conversation_id, actor_kind, actor_id)"])

table("ag_attachment","私有附件",["R07","R13","R14","R17"],[
    ID, ("owner_kind","varchar(8) NOT NULL CHECK (owner_kind IN ('MEMBER','STAFF'))","上传主体类型"),
    ("owner_id","bigint NOT NULL CHECK (owner_id > 0)","上传主体 ID"),
    ("purpose","varchar(24) NOT NULL CHECK (purpose IN ('CHAT_IMAGE','KNOWLEDGE','RECORDING','SPEECH'))","用途决定限额和留存"),
    ("object_key","varchar(512) NOT NULL UNIQUE","私有桶内随机对象键，不接受任意 URL"),
    ("original_name","varchar(255) NOT NULL","展示文件名，不参与拼接存储路径"),
    ("mime_type","varchar(100) NOT NULL","实际字节检测所得 MIME"),
    ("byte_size","bigint NOT NULL CHECK (byte_size > 0)","实际字节数"),
    ("sha256","char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$')","实际内容哈希"),
    ("duration_ms","integer CHECK (duration_ms BETWEEN 1 AND 60000)","录音时长，TTS 限制另由配置控制"),
    state("state","UPLOADING READY FAILED DELETING DELETED","UPLOADING"),
    ("retention_until","timestamptz","普通图片 90 天；音频 7 天；知识由引用决定"),
    ("storage_deleted_at","timestamptz","对象物理删除时间")],
    indexes=["CREATE INDEX ix_attachment_gc ON ag_attachment(retention_until, id) WHERE state = 'READY';"])

table("ag_run","独立生成任务",["R03","R18"],[
    ID, CONV, ("trigger_message_id","uuid NOT NULL REFERENCES ag_message(id)","用户消息；重试沿用原消息"),
    ("attempt","integer NOT NULL DEFAULT 1 CHECK (attempt > 0)","每条触发消息第几次生成"),
    ("retry_of_run_id","uuid REFERENCES ag_run(id)","上一失败/停止的生成"),
    state("state","QUEUED RUNNING COMPLETED STOP_REQUESTED STOPPED FAILED INTERRUPTED","QUEUED"),
    ("conversation_version","bigint NOT NULL","接管前后版本栅栏"),
    ("memory_epoch","bigint NOT NULL","检查点与会话删除世代一致"),
    ("checkpoint_thread_id","varchar(128) NOT NULL","独立图 schema 的 thread_id，不存令牌"),
    ("lease_owner","varchar(100)","工作进程租约"),
    ("lease_until","timestamptz","工作进程续约"),
    ("fencing_token","bigint NOT NULL DEFAULT 1","旧工作进程不得续写"),
    ("started_at","timestamptz","开始执行时间"),
    ("finished_at","timestamptz","终止时间"),
    ("error_code","varchar(80)","对外稳定错误码，不含供应商原始敏感响应")],
    constraints=["UNIQUE(trigger_message_id, attempt)"],
    indexes=["CREATE UNIQUE INDEX uq_run_active_conversation ON ag_run(conversation_id) WHERE state IN ('QUEUED','RUNNING','STOP_REQUESTED');",
             "CREATE INDEX ix_run_queue ON ag_run(created_at, id) WHERE state = 'QUEUED';"])

table("ag_generation_slot","全局生成名额",["R18"],[
    ("slot_no","smallint PRIMARY KEY CHECK (slot_no BETWEEN 1 AND 3)","固定三个名额"),
    ("run_id","uuid UNIQUE REFERENCES ag_run(id)","被占用的任务"),
    ("lease_until","timestamptz","异常时等待请求超时及清算后回收"),
    ("fencing_token","bigint NOT NULL DEFAULT 1","每次领取递增")])

table("ag_action","不可变业务确认操作",["R08","R09","R11","R12","R18"],[
    ID, CONV, MEMBER,
    ("action_type","varchar(24) NOT NULL CHECK (action_type IN ('ADD_CART','CREATE_ORDER','CANCEL_ORDER','CREATE_RETURN'))","四种受控写操作"),
    state("state","PENDING_CONFIRMATION EXECUTING SUCCEEDED FAILED UNKNOWN CANCELLED EXPIRED STALE","PENDING_CONFIRMATION"),
    ("conversation_version","bigint NOT NULL","准备时会话版本"),
    ("parameters","jsonb NOT NULL","已规范化不可变业务参数；不含令牌"),
    ("parameters_hash","char(64) NOT NULL CHECK (parameters_hash ~ '^[0-9a-f]{64}$')","按共享规范化算法计算"),
    ("quote_id","uuid","Java 报价；加车/取消也可带准备快照"),
    ("operation_no","uuid NOT NULL UNIQUE","等于 action_id；Java/MySQL 唯一操作编号"),
    ("expires_at","timestamptz NOT NULL","默认准备后 5 分钟"),
    ("confirmed_at","timestamptz","有效用户点击时间"),
    ("result","jsonb","业务结果与脱敏卡片，不包含原始令牌"),
    ("error_code","varchar(80)","已知失败/失效原因"),
    ("reconcile_after","timestamptz","UNKNOWN 使用原编号核对时间")],
    indexes=["CREATE INDEX ix_action_reconcile ON ag_action(reconcile_after) WHERE state IN ('EXECUTING','UNKNOWN');",
             "CREATE INDEX ix_action_conversation ON ag_action(conversation_id, created_at DESC);"])

table("ag_handoff","排队与接待请求",["R06"],[
    ID, CONV, MEMBER,
    state("state","WAITING CLAIMED WITHDRAWN CLOSED","WAITING"),
    ("claimed_admin_id","bigint REFERENCES ag_staff_profile(admin_id)","领取人；转交后跟随会话"),
    ("reason","varchar(500)","转人工原因"),
    ("claimed_at","timestamptz","领取时间"),
    ("finished_at","timestamptz","结束时间")],
    indexes=["CREATE UNIQUE INDEX uq_handoff_active ON ag_handoff(conversation_id) WHERE state IN ('WAITING','CLAIMED');",
             "CREATE INDEX ix_handoff_wait ON ag_handoff(created_at, id) WHERE state = 'WAITING';"])

table("ag_event_outbox","持久事件与推送发件箱",["R03","R06","R18"],[
    ("event_id","bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY","全局单调游标，允许间隙，API 为十进制字符串"),
    ("conversation_id","uuid REFERENCES ag_conversation(id)","会话事件必填；管理任务事件可空"),
    ("conversation_version","bigint","会话版本；旧生成不可继续展示"),
    ("event_type","varchar(80) NOT NULL","SSE 类型，见事件契约"),
    ("visibility","varchar(24) NOT NULL CHECK (visibility IN ('CONVERSATION','STAFF_ONLY','PERMISSION'))","每次订阅/补传重新鉴权"),
    ("required_permission","varchar(100)","全局管理事件要求的权限"),
    ("payload","jsonb NOT NULL","只包含该受众可见内容；内部备注单独 STAFF_ONLY"),
    ("published_at","timestamptz","向通知通道发布成功时间；补传不依赖 Redis"),
    ("publish_attempts","integer NOT NULL DEFAULT 0 CHECK (publish_attempts >= 0)","有限重试与监控"),
    ("retention_until","timestamptz NOT NULL","建议事件补传保留 7 天，消息仍按 90 天")],
    constraints=["CHECK ((visibility = 'PERMISSION' AND required_permission IS NOT NULL) OR (visibility <> 'PERMISSION' AND conversation_id IS NOT NULL))"],
    indexes=["CREATE INDEX ix_outbox_unpublished ON ag_event_outbox(event_id) WHERE published_at IS NULL;",
             "CREATE INDEX ix_outbox_replay ON ag_event_outbox(conversation_id, event_id);"], mutable=False)

table("ag_model_profile","按能力隔离的模型配置",["R16"],[
    ID, ("name","varchar(100) NOT NULL","配置显示名"),
    ("capability","varchar(20) NOT NULL CHECK (capability IN ('DIALOG','EMBEDDING','RERANK','VISION','ASR','TTS'))","模型能力"),
    ("provider","varchar(80) NOT NULL","供应商标识"),
    ("model_name","varchar(160) NOT NULL","候选模型名，以实际接入验收为准"),
    ("endpoint_ref","varchar(160) NOT NULL","受控端点注册名，不让普通配置绕过出站白名单"),
    ("secret_ref","varchar(200) NOT NULL","秘密管理中的引用，普通读取接口不返回"),
    ("public_options","jsonb NOT NULL DEFAULT '{}'::jsonb","允许字段白名单，禁止放 API 密钥"),
    ("enabled","boolean NOT NULL DEFAULT false","实测通过后启用"),
    ("is_active","boolean NOT NULL DEFAULT false","每种能力唯一活动配置")],
    constraints=["CHECK (NOT is_active OR enabled)"],
    indexes=["CREATE UNIQUE INDEX uq_model_active_capability ON ag_model_profile(capability) WHERE is_active;"])

table("ag_prompt_version","提示词版本",["R16"],[
    ID, ("prompt_key","varchar(80) NOT NULL","节点/职责标识"),
    ("version_no","integer NOT NULL CHECK (version_no > 0)","独立版本序号"),
    ("content","text NOT NULL","后台受权限保护；不向买家输出"),
    state("state","DRAFT PUBLISHED RETIRED","DRAFT"),
    ("sha256","char(64) NOT NULL","模板摘要"),
    ("created_by","bigint NOT NULL","Java 员工 ID"),
    ("published_at","timestamptz","发布时间")],
    constraints=["UNIQUE(prompt_key,version_no)"],
    indexes=["CREATE UNIQUE INDEX uq_prompt_published ON ag_prompt_version(prompt_key) WHERE state='PUBLISHED';"])

table("ag_kb_document","知识逻辑文档",["R05","R07"],[
    ID, ("title","varchar(200) NOT NULL","知识标题"),
    ("category","varchar(80) NOT NULL","配送/售后/FAQ/商品说明"),
    ("visibility","varchar(16) NOT NULL CHECK (visibility IN ('MEMBER','STAFF'))","检索前授权，回答前复核"),
    ("scope","jsonb NOT NULL DEFAULT '{}'::jsonb","适用分类/商品等范围，空对象表示全站"),
    ("enabled","boolean NOT NULL DEFAULT true","停用后所有新回答立即排除"),
    ("active_version_id","uuid","活动版本，建表后补同文档复合外键"),
    ("created_by","bigint NOT NULL","维护员工 ID")])

table("ag_kb_version","知识不可变版本",["R05","R07"],[
    ID, ("document_id","uuid NOT NULL REFERENCES ag_kb_document(id)","所属文档"),
    ("version_no","integer NOT NULL CHECK (version_no > 0)","文档内版本"),
    state("state","DRAFT PARSING PREVIEW_READY INDEXING READY FAILED RETIRED","DRAFT"),
    ("source_attachment_id","uuid REFERENCES ag_attachment(id)","原始 PDF/DOCX；纯文本可空"),
    ("source_text","text","原始文本或编辑版原文"),
    ("parsed_text","text","预览确认后的文本"),
    ("source_sha256","char(64) NOT NULL","内容哈希"),
    ("embedding_profile_id","uuid REFERENCES ag_model_profile(id)","向量配置版本关联"),
    ("embedding_dimension","integer CHECK (embedding_dimension > 0)","候选 1024，以实际验收锁定"),
    ("index_collection","varchar(160)","含模型/维度代号的 Milvus 暂存集合"),
    ("chunk_count","integer NOT NULL DEFAULT 0 CHECK (chunk_count >= 0)","成功切片数"),
    ("quality_report","jsonb","质检题集结果，发布前检查"),
    ("published_at","timestamptz","活动切换时间"),
    ("created_by","bigint NOT NULL","创建员工 ID")],
    constraints=["UNIQUE(document_id,version_no)","UNIQUE(document_id,id)"])

table("ag_kb_chunk","知识父子切片",["R05","R07"],[
    ID, ("version_id","uuid NOT NULL REFERENCES ag_kb_version(id)","所属知识版本"),
    ("ordinal","integer NOT NULL CHECK (ordinal >= 0)","版本内序号"),
    ("chunk_kind","varchar(8) NOT NULL CHECK (chunk_kind IN ('PARENT','CHILD'))","子块召回、父块供回答"),
    ("parent_chunk_id","uuid","同版本父切片；复合外键"),
    ("content","text NOT NULL","可重建向量的可靠文本"),
    ("section_path","jsonb NOT NULL DEFAULT '[]'::jsonb","章节路径"),
    ("page_start","integer CHECK (page_start > 0)","源文档可验证页码；无页码则空"),
    ("page_end","integer","源结束页码"),
    ("vector_key","varchar(100)","Milvus 子切片主键"),
    ("metadata","jsonb NOT NULL DEFAULT '{}'::jsonb","来源定位和范围")],
    constraints=["UNIQUE(version_id,ordinal)","UNIQUE(version_id,id)",
        "FOREIGN KEY(version_id,parent_chunk_id) REFERENCES ag_kb_chunk(version_id,id) DEFERRABLE INITIALLY DEFERRED",
        "CHECK ((chunk_kind='PARENT' AND parent_chunk_id IS NULL) OR (chunk_kind='CHILD' AND parent_chunk_id IS NOT NULL))",
        "CHECK (page_end IS NULL OR (page_start IS NOT NULL AND page_end >= page_start))"], mutable=False)

table("ag_attachment_ref","附件引用与售后保护",["R07","R13","R17"],[
    ID, ("attachment_id","uuid NOT NULL REFERENCES ag_attachment(id)","附件"),
    ("ref_kind","varchar(20) NOT NULL CHECK (ref_kind IN ('MESSAGE','KB_VERSION','RETURN_PROOF','SPEECH'))","引用类型"),
    ("message_id","uuid REFERENCES ag_message(id)","消息或播报对应消息"),
    ("kb_version_id","uuid REFERENCES ag_kb_version(id)","知识版本"),
    ("action_id","uuid REFERENCES ag_action(id)","售后准备/执行关联；保护 UNKNOWN"),
    ("return_apply_id","bigint","Java 售后申请 ID，无跨库外键"),
    ("protected","boolean NOT NULL DEFAULT false","活动售后或结果未知时禁止物理删除"),
    ("released_at","timestamptz","释放引用的时间")],
    constraints=["CHECK ((ref_kind IN ('MESSAGE','SPEECH') AND message_id IS NOT NULL AND kb_version_id IS NULL AND action_id IS NULL) OR (ref_kind='KB_VERSION' AND kb_version_id IS NOT NULL AND message_id IS NULL AND action_id IS NULL) OR (ref_kind='RETURN_PROOF' AND action_id IS NOT NULL AND message_id IS NULL AND kb_version_id IS NULL))"],
    indexes=["CREATE UNIQUE INDEX uq_attachment_message_ref ON ag_attachment_ref(attachment_id,message_id,ref_kind) WHERE released_at IS NULL AND message_id IS NOT NULL;",
             "CREATE UNIQUE INDEX uq_attachment_kb_ref ON ag_attachment_ref(attachment_id,kb_version_id) WHERE released_at IS NULL AND kb_version_id IS NOT NULL;",
             "CREATE UNIQUE INDEX uq_attachment_action_ref ON ag_attachment_ref(attachment_id,action_id) WHERE released_at IS NULL AND action_id IS NOT NULL;",
             "CREATE INDEX ix_attachment_active_refs ON ag_attachment_ref(attachment_id) WHERE released_at IS NULL;"])

table("ag_job","后台任务与清理恢复记录",["R07","R17","R18"],[
    ID, ("job_type","varchar(32) NOT NULL CHECK (job_type IN ('KB_PARSE','KB_INDEX','RETENTION_PREVIEW','RETENTION_EXECUTE','BACKUP_VERIFY','RESTORE_VERIFY','ATTACHMENT_DELETE','ACTION_RECONCILE'))","任务种类"),
    state("state","QUEUED RUNNING SUCCEEDED FAILED CANCELLED","QUEUED"),
    ("dedupe_key","varchar(180) NOT NULL","同版本/同批次不重复执行"),
    ("parameters","jsonb NOT NULL","类型校验后的 ID 与配置版本，不存凭据"),
    ("result","jsonb","数量、游标、质检或恢复核对结果"),
    ("attempts","integer NOT NULL DEFAULT 0 CHECK (attempts >= 0)","有限重试"),
    ("max_attempts","integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10)","重试上限"),
    ("progress","smallint NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100)","已处理进度"),
    ("lease_until","timestamptz","工作进程租约"),
    ("error_code","varchar(80)","稳定失败码"),
    ("error_message","varchar(500)","已脱敏失败原因"),
    ("created_by","bigint","发起员工 ID；系统任务可空")],
    constraints=["UNIQUE(job_type,dedupe_key)"],
    indexes=["CREATE INDEX ix_job_queue ON ag_job(created_at,id) WHERE state='QUEUED';"])

table("ag_model_call","调用用量与错误",["R16","R18"],[
    ID, ("run_id","uuid REFERENCES ag_run(id)","对话生成可关联；独立媒体任务可空"),
    ("job_id","uuid REFERENCES ag_job(id)","知识/清理等任务关联"),
    ("profile_id","uuid NOT NULL REFERENCES ag_model_profile(id)","配置"),
    ("profile_version","bigint NOT NULL","调用时配置版本"),
    ("prompt_version_id","uuid REFERENCES ag_prompt_version(id)","使用的提示词版本"),
    ("provider_request_id","varchar(160)","供应商请求追踪号"),
    ("capability","varchar(20) NOT NULL","按能力统计"),
    ("status","varchar(16) NOT NULL CHECK (status IN ('SUCCEEDED','FAILED','CANCELLED','UNKNOWN'))","调用结果"),
    ("input_tokens","bigint CHECK (input_tokens >= 0)","未报告时为空，不伪记为 0"),
    ("output_tokens","bigint CHECK (output_tokens >= 0)","未报告时为空"),
    ("billable_units","numeric(20,6) CHECK (billable_units >= 0)","语音时长、字符或其他计费单位"),
    ("estimated_cost","numeric(20,6) CHECK (estimated_cost >= 0)","估算值；不是供应商最终账单"),
    ("latency_ms","bigint NOT NULL CHECK (latency_ms >= 0)","耗时"),
    ("error_code","varchar(80)","稳定错误码")],
    indexes=["CREATE INDEX ix_model_call_time ON ag_model_call(created_at DESC,capability);"], mutable=False)

table("ag_audit_log","受限操作审计",["R01","R06","R16","R17","R18"],[
    ID, ("actor_kind","varchar(16) NOT NULL","MEMBER/STAFF/SYSTEM"),
    ("actor_id","bigint","责任主体；系统可空"),
    ("operation","varchar(100) NOT NULL","领取/发布/查看受限资料/执行等"),
    ("resource_type","varchar(60) NOT NULL","资源类型"),
    ("resource_id","varchar(120) NOT NULL","资源主键"),
    ("request_id","uuid NOT NULL","贯穿 API、任务和 Java 的追踪号"),
    ("outcome","varchar(24) NOT NULL","成功/拒绝/失败"),
    ("details","jsonb NOT NULL DEFAULT '{}'::jsonb","脱敏摘要与版本差异，禁止秘密和整段私聊")],
    indexes=["CREATE INDEX ix_audit_resource ON ag_audit_log(resource_type,resource_id,created_at DESC);",
             "CREATE INDEX ix_audit_request ON ag_audit_log(request_id);"], mutable=False)

table("ag_feedback","回答反馈",["R15"],[
    ID, ("message_id","uuid NOT NULL REFERENCES ag_message(id)","只能反馈本人会话中的可见 AI 回答"),
    MEMBER, ("vote","smallint NOT NULL CHECK (vote IN (-1,1))","赞为 1，踩为 -1"),
    ("reason","varchar(500)","可选理由")], constraints=["UNIQUE(member_id,message_id)"])

table("ag_setting","版本化业务与运维配置",["R16","R17"],[
    ("key","varchar(100) PRIMARY KEY","只允许受控键；秘密禁止入此表"),
    ("value","jsonb NOT NULL","用对应配置 schema 校验"),
    ("updated_by","bigint NOT NULL","员工 ID")])

table("ag_context_summary","可清理上下文总结",["R02","R17","R18"],[
    ID, CONV, ("memory_epoch","bigint NOT NULL","旧 epoch 的总结禁止使用"),
    ("through_seq","bigint NOT NULL CHECK (through_seq > 0)","覆盖至消息序号"),
    ("summary","text NOT NULL","不作为业务事实，随源消息删除失效"),
    ("retention_until","timestamptz NOT NULL","不长于最早源消息的保留期")],
    constraints=["UNIQUE(conversation_id,memory_epoch,through_seq)"], mutable=False)

table("ag_request_dedupe","普通写接口幂等记录",["R03","R06","R18"],[
    ("scope","varchar(120) NOT NULL","接口操作作用域"),
    ("actor_kind","varchar(8) NOT NULL CHECK (actor_kind IN ('MEMBER','STAFF'))","主体类型"),
    ("actor_id","bigint NOT NULL","当前身份"),
    ("idempotency_key","uuid NOT NULL","请求头 Idempotency-Key"),
    ("request_hash","char(64) NOT NULL","同键不同请求返回 409"),
    ("response_ref","jsonb NOT NULL","已提交结果引用，重放前重新鉴权"),
    ("expires_at","timestamptz NOT NULL","建议普通请求至少保留 24 小时；业务幂等由 MySQL 永久约束")],
    constraints=["PRIMARY KEY(scope,actor_kind,actor_id,idempotency_key)"], mutable=False)

table("ag_erasure_tombstone","删除标记",["R02","R17","R18"],[
    ("resource_type","varchar(40) NOT NULL","CONVERSATION/MESSAGE/ATTACHMENT"),
    ("resource_id","varchar(120) NOT NULL","最小必要不可恢复标识"),
    ("memory_epoch","bigint","会话删除世代"),
    ("erased_at","timestamptz NOT NULL DEFAULT now()","所有消费者与恢复流程必须检查")],
    constraints=["PRIMARY KEY(resource_type,resource_id)"], mutable=False)

POST_EXTRA = """
ALTER TABLE ag_message ADD CONSTRAINT fk_message_run FOREIGN KEY(run_id) REFERENCES ag_run(id);
ALTER TABLE ag_kb_document ADD CONSTRAINT fk_kb_active_same_document
    FOREIGN KEY(id,active_version_id) REFERENCES ag_kb_version(document_id,id)
    DEFERRABLE INITIALLY DEFERRED;
INSERT INTO ag_generation_slot(slot_no) VALUES (1),(2),(3);
-- langgraph_checkpoint schema 由锁定版本的 AsyncPostgresSaver.setup 管理。
-- 不手写、替换或删除组件内部检查点表；连接 search_path 须明确限定。
CREATE SCHEMA langgraph_checkpoint;
"""

MYSQL = """
-- 设计草案 v0.1，目标 MySQL 8.0.35；本文件未执行。
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
"""

def generate():
    global TABLES, MYSQL, POST_EXTRA
    from architecture_contracts import extend_database
    TABLES, MYSQL, POST_EXTRA = extend_database(TABLES, MYSQL, POST_EXTRA)
    dest = ROOT / "database"
    dest.mkdir(exist_ok=True)
    sql = ["-- 设计草案 v0.2，PostgreSQL 16；尚未执行。", "-- 目标：独立 mall_agent 数据库的 public schema。", "-- UUID 由服务生成，不依赖扩展；所有日期为 timestamptz。", "BEGIN;"]
    dictionary = ["# 数据库设计", "", "设计版本 v0.1，2026-11-10。需求基线 1.1。设计脚本未执行，未创建任何数据库或容器。", "",
      "## 数据归属", "", "商城 MySQL 8.0.35 保存商品、计价、库存、购物车、订单、售后、模拟物流和最终业务操作结果。独立 PostgreSQL 16 保存会话、接待、知识原文与版本、任务、模型配置引用、事件和审计。Milvus 是可重建检索索引；MinIO 保存私有字节；Redis 保存在线状态、限流和通知。跨服务不建物理外键，不把 PostgreSQL 操作状态当作订单最终事实。", "",
      "## 关系图", "", "详见 er.mmd。ag_conversation 关联消息、任务、操作、接待和事件；知识文档通过 active_version_id 指向自己的活动版本；附件由引用表保护。Java 身份 ID 只保存标识，运行时复核状态与权限。", "",
      "## 通用约定", "", "- Java bigint ID 在接口中一律十进制字符串；PostgreSQL 自有主键 UUID。", "- 金额存 decimal/numeric，接口使用两位小数字符串；不得用浮点参与金额计算。", "- PostgreSQL 使用 timestamptz；MySQL 新列 DATETIME(6) 约定 UTC；页面按 Asia/Shanghai 展示，接口 RFC3339。旧 Java Date 列通过适配器按已核实连接时区解释，不批量重写历史。", "- 可变记录 version 由应用在条件更新中递增，updated_at 同事务更新。所有普通写接口 expected_version 不等返回 409。", "- 数据库账号按服务隔离；审计表应用账号仅 INSERT/SELECT，留存清理由独立账号执行。DDL 不自动创建账号或授予权限。", "- 客户端消息 UUID 在会话内唯一；重放还必须核验发送主体与内容哈希。API 幂等键和消息 UUID 分别校验，同键不同主体或内容返回 409。", "- 姓名、地址、电话使用按权限返回的业务卡片；公共运行日志仅记录标识、状态和脱敏摘要。", "",
      "## PostgreSQL 数据字典", ""]
    for t in TABLES:
        defs = ["  " + n + " " + typ for n, typ, _ in t["fields"]] + ["  " + c for c in t["constraints"]]
        sql.append("CREATE TABLE " + t["name"] + " (\n" + ",\n".join(defs) + "\n);")
        sql.append("COMMENT ON TABLE " + t["name"] + " IS '" + t["title"] + "';")
        for n, typ, desc in t["fields"]:
            sql.append("COMMENT ON COLUMN " + t["name"] + "." + n + " IS '" + desc.replace("'","''") + "';")
        sql.extend(t["indexes"])
        dictionary += ["### " + t["name"] + " " + t["title"], "", "对应需求：" + "、".join(t["requirements"]), "",
                       "| 字段 | SQL 类型与约束 | 说明 |", "|---|---|---|"]
        dictionary += ["| " + n + " | " + typ.replace("|","/") + " | " + desc + " |" for n,typ,desc in t["fields"]]
        dictionary += ["", "表约束：" + ("；".join(t["constraints"]) or "主键及列级约束。"), "",
                       "索引：" + (" ".join(t["indexes"]) or "使用主键与唯一约束索引。"), ""]
    sql += [POST_EXTRA,"COMMIT;"]
    (dest/"postgresql-v1.sql").write_text("\n\n".join(sql),encoding="utf-8")
    (dest/"mysql-v1.sql").write_text(MYSQL.strip()+"\n",encoding="utf-8")
    (dest/"table-catalog.json").write_text(json.dumps(TABLES,ensure_ascii=False,indent=2),encoding="utf-8")
    dictionary += ["## MySQL 增量设计", "",
      "mysql-v1.sql 包含 7 张新增表及对 oms_order_return_apply 的增量扩展。报价和操作结果归属 Java。新增表的完整字段、索引、检查约束和外键以该脚本为准；无需重建现有 76 张表。", "",
      "| 对象 | 作用 | 关键约束 |", "|---|---|---|",
      "| oms_agent_quote | 不改变购物车的准确报价与准备快照 | quote_id 主键；会员绑定；参数哈希；5 分钟有效期 |",
      "| oms_agent_operation | 加车/下单/取消/退货的最终幂等事实 | operation_no 唯一；与业务变更同一事务 |",
      "| oms_after_sale_rule_version | 不可变售后规则版本 | rule_code + version_no 唯一 |",
      "| oms_order_return_apply 新列 | 明细定位、申请金额、规则、独立处理阶段与版本 | 新申请应用层强制 order_item_id 非空；历史允许空值 |",
      "| oms_return_proof_reference | 私有售后凭证引用 | 申请 + 附件引用唯一；不存任意公网地址 |",
      "| oms_shipment / oms_shipment_event | 每单一份模拟物流与多个节点 | 模拟标记强制为 1；事件按时间与 ID 排序 |",
      "| oms_agent_audit | 物流编辑、售后阶段及业务操作审计 | 与对应变更同事务写入；受限读取 |", "",
      "## 事务及并发设计", "",
      "1. 发送消息：锁会话并校验身份、版本和状态 → 检查消息 UUID/请求幂等 → 分配 seq → 写消息、任务、outbox、幂等引用 → 提交 → 异步通知。相同消息重放返回已有任务；内容改变返回 409。生成中第二条新消息返回 RUN_ALREADY_ACTIVE，不先写入一个无法执行的重复任务。人工模式允许正常买家消息。", "",
      "2. 人工领取：按固定顺序锁员工资料和会话 → 验证在线、权限、接待容量 → 条件领取 handoff → 会话置 HUMAN_ACTIVE 并递增版本 → 停止任务、失效待确认卡片 → 写事件及审计 → 提交。转交锁两个员工时按 ID 排序。已执行业务继续核对。心跳失效不释放接待归属，只展示离线，主管可审计转交。", "",
      "3. 用户确认：锁会话和 action 并核验版本/期限/归属 → action 从待确认变 EXECUTING → 提交后调 Java。Java 开启事务，以 operation_no 先 INSERT IN_TX 行取得唯一约束锁，再校验同编号哈希与会员，锁目标资源并执行，写最终结果 SUCCEEDED 或无副作用的 REJECTED，与业务同事务提交。IN_TX 不得被正常代码提交，查询只返回终态；检测到已提交 IN_TX 视作异常待人工核对。应用故障整笔回滚。禁止独立提交成功日志。", "",
      "4. Python 调用超时标记 UNKNOWN，并仅用原 operation_no 查询。Java 暂未查询到已提交记录不等于确定失败，可能仍有在途事务；持续显示核对中直到明确结果或人工查明。恢复与重试不创建新编号，不自动重放写请求。业务幂等记录不随聊天清理。", "",
      "5. 库存、优惠券和积分继续使用 Java 条件更新与事务；底层已有 OrderResourceService，设计要求复验其完整链路。报价和下单共用计算器，最终结果与确认快照按明细、币种和分项金额比较，变化则失效，重新确认。", "",
      "6. 售后按订单/明细锁序核验数量；待处理、已批准、收到货和模拟退款完成均占用可退数量，拒绝释放占用。按原始订单实付分摊，累计申请不超过原行实付。金额取整及 7 天口径的建议见设计决策 D03/D04。", "",
      "7. 资料上传先保存私有附件。售后准备建立 protected 引用，Java 同事务保存该引用的标识、摘要和申请。跨库无法原子提交，Python 对 EXECUTING/UNKNOWN 引用持续保护，用原操作结果对账后绑定申请；明确未执行才释放。定时清理不能仅按短期租约删除结果未知的凭证。Java 下载证据通过服务身份鉴权，向员工逐次授权。", "",
      "## 事件、检查点与删除", "",
      "outbox 与业务可见状态同事务；事件按受众过滤，不能把内部备注混入买家事件。建议 delta 每 200ms 或 64 字合批入 outbox，最终完整消息再次落库。事件 ID 为全局游标，存在间隙，重连无需连续编号。超过事件保留期返回 410 EVENT_CURSOR_EXPIRED，前端拉取快照及最近历史后使用新游标。", "",
      "同会话生成由部分唯一索引串行化；全局三个 ag_generation_slot 使用 FOR UPDATE SKIP LOCKED 领取。工作进程失去租约停止后续工具和事件写入。崩溃名额在供应商超时窗口结束、旧 worker 已隔离后回收，不能仅租约过期就立刻启动第四个供应商请求。供应商不可确认中止时保守占用至超时。", "",
      "LangGraph 独立 schema 的内部表由锁定组件维护；身份秘密不进入检查点。删除/清理首先标记内容不可见、递增 memory_epoch、写 tombstone，随后清除总结、对应检查点、缓存、向量与无保护附件。任何 checkpoint 恢复需核对 epoch 和源消息仍可用。消息删除与持有业务凭证的附件引用分开处理。", "",
      "## Milvus 及对象存储", "",
      "子切片键为 UUID；字段包含 document_id、version_id、parent_chunk_id、visibility、商品/分类范围、embedding_profile_id、dimension、text 和向量。按模型及维度建立独立集合，先建新版本再切活动版本；回答前回 PostgreSQL 校验启用、活动版本与范围，防止最终一致删除造成旧资料泄漏。保留原件及切片文本，可重建索引。Milvus 专用 etcd/对象存储与应用附件桶隔离。", "",
      "## 迁移与回滚", "",
      "先保存业务备份和源码/构建基线，在隔离库执行新增表/列迁移；再升级 Java 桥接并验证旧页面兼容；随后安装 PostgreSQL schema 和应用。开发阶段在迁移系统记录脚本校验和。DDL 文件不承诺重复执行，禁止拿 IF NOT EXISTS 掩盖版本漂移。", "",
      "历史售后仅在订单中商品唯一且数量/金额关系可核实时回填明细；歧义保持空值。process_stage 为空的历史记录展示原状态，不伪造收到退货与退款完成的细节。禁止自动批量改写历史申请。", "",
      "回滚应用时保留新增业务数据和兼容列，停用新增入口及 worker；不自动 DROP 表、不撤销已成功订单。备份同时覆盖 MySQL 操作事实、PostgreSQL 消息和检查点、MinIO 原件、配置引用与删除标记；恢复先禁止写工具，完成操作对账和删除标记重放，再放开确认。", "",
      "## 留存与维护", "",
      "消息/普通图片默认 90 天；原始录音/TTS 缓存 7 天；事件建议 7 天。知识原件按活动版本与引用保留；进行中的售后证据保护。操作结果、审计和删除标记使用独立策略，设计草案默认不自动清理，需按追溯需求另行配置。清理必须先试算、绑定策略版本和执行批次、支持重入与失败重试。", "",
      "## 验证边界", "",
      "本次只生成设计脚本并执行静态一致性检查；未启动本地 MySQL，未连接生产执行 DDL，未安装 PostgreSQL/Milvus。SQL 上线前仍需目标版本隔离库语法、迁移、并发及恢复验收。"]
    (ROOT/"02-数据库设计.md").write_text("\n".join(dictionary)+"\n",encoding="utf-8")
    er = ["erDiagram"]
    edges = [("ag_conversation","ag_message","contains"),("ag_conversation","ag_run","generates"),
      ("ag_conversation","ag_action","prepares"),("ag_conversation","ag_handoff","queues"),
      ("ag_conversation","ag_staff_note","notes"),("ag_conversation","ag_read_cursor","reads"),
      ("ag_conversation","ag_event_outbox","events"),("ag_kb_document","ag_kb_version","versions"),
      ("ag_kb_version","ag_kb_chunk","chunks"),("ag_attachment","ag_attachment_ref","references"),
      ("ag_message","ag_feedback","feedback"),("ag_run","ag_model_call","usage"),
      ("oms_agent_quote","oms_agent_operation","confirms"),("oms_order_item","oms_order_return_apply","returns"),
      ("oms_order_return_apply","oms_return_proof_reference","proofs"),("oms_order","oms_shipment","shipment"),
      ("oms_shipment","oms_shipment_event","timeline")]
    for a,b,label in edges: er.append("    "+a+" ||--o{ "+b+" : "+label)
    (dest/"er.mmd").write_text("\n".join(er)+"\n",encoding="utf-8")
    return len(TABLES)

if __name__ == "__main__":
    print("PostgreSQL tables:",generate())
