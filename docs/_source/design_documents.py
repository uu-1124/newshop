"""Compose human-readable design documents from the shared contracts."""
from pathlib import Path
import json
from docx import Document

ROOT=Path(__file__).resolve().parents[1]
FENCE=chr(96)*3
PAGES=[
 ("B01","买家客服入口","buyer","R01 R04","我的、商品详情、本人订单详情的客服入口；未登录返回现有登录页，登录后只恢复原上下文 ID。"),
 ("B02","聊天与会话历史","buyer","R02 R03 R05 R06 R13 R14 R15","历史、新建、重命名、删除、已读、流式消息、停止/重试、来源、图片、语音、反馈、人工入口。"),
 ("B03","商品比较与规格选择","buyer","R04 R08","聊天商品卡片；比较缺失字段明确标为未提供；选择 SKU/数量后生成确认卡片，按钮确认才加车。"),
 ("B04","报价与下单确认","checkout","R09 R18","选择已有地址、优惠券和积分，读取最终报价；明细、各项折扣、运费、应付与有效期完整展示；成功跳转现有模拟支付页面。"),
 ("B05","订单物流与售后","orders","R10 R11 R12","本人多订单选择、实际状态、模拟物流、取消确认、按订单明细退货、剩余数量、凭证与处理结果。"),
 ("A01","人工接待工作台","staff","R01 R06","等待/我的/历史、原子领取、人工消息、会员业务摘要、独立备注、在线状态、转交与关闭。"),
 ("A02","知识维护与版本发布","knowledge","R05 R07","资料列表、文本/PDF/DOCX、新版本、解析预览、构建任务、质量检查、发布与禁用。"),
 ("A03","模型提示词与用量","models","R16","按能力配置、脱敏状态、提示词草稿/发布、调用量、错误和延迟；供应商接入状态明确。"),
 ("A04","员工配置留存与审计","governance","R06 R16 R17 R18","客服容量、留存配置、影响试算、执行清理、任务状态、受限审计、备份恢复核对记录。"),
 ("A05","模拟物流与人工售后","operations","R10 R12","物流资料与节点维护、节点编辑审计、售后审批、收货登记、模拟退款完成，分权限显示按钮。")
]
def write(name,s): (ROOT/name).write_text(s.strip()+"\n",encoding="utf-8")

def generate():
    ops=json.loads((ROOT/"api/operation-catalog.json").read_text(encoding="utf-8"))
    tables=json.loads((ROOT/"database/table-catalog.json").read_text(encoding="utf-8"))
    reqdoc=next(ROOT.glob("*.docx"))
    doc=Document(reqdoc)
    reqs=[dict(zip(["id","priority","scope","mapping"],[c.text for c in r.cells])) for r in doc.tables[16].rows[1:]]
    write("00-设计导航.md", """
# 商城智能体设计导航

本目录将需求基线、前端 UI、数据库和接口设计放在一起。设计 v0.1 对应 2026-11-10 的需求基线 1.1，覆盖 R01 至 R18 的完整范围。它是可评审设计稿，尚未进入商城功能实现或部署验收。

建议先用浏览器打开 index.html，可查看设计说明、接口目录和可点击 UI 原型；所有页面离线可用。也可直接打开 ui/index.html。

| 文件或目录 | 内容 |
|---|---|
| 商城智能体需求分析与技术设计.docx | 原需求基线 1.1，原文件未改动 |
| 01-前端UI设计.md | 页面、控件、状态、权限和响应式规范 |
| 02-数据库设计.md | 25 张 PostgreSQL 表的字段字典、Java/MySQL 扩展、事务与清理设计 |
| 03-接口设计.md | 鉴权、错误、幂等、分页、事件及具体接口目录 |
| 04-需求设计映射.md | 每个 R 编号对应的页面、数据、接口与原验收场景 |
| 05-设计决策与环境.md | 设计建议、已确定约束、参考借鉴及 MySQL 检查结果 |
| ui/ | 买家端与管理端可点击原型，只有示例状态，不连接商城服务 |
| api/openapi.json | OpenAPI 3.1 机器可读契约，含具体请求与返回 schema |
| api/sse-events.schema.json | 流式 data JSON 的事件契约 |
| database/postgresql-v1.sql | 新客服库设计脚本；未执行 |
| database/mysql-v1.sql | 商城增量表/字段设计脚本；未执行 |
| database/er.mmd | 可供 Mermaid 渲染的实体关系图 |
| 06-设计检查记录.md | 静态一致性、原型逻辑检查结果与未验证范围 |
| _source/ | 设计生成及一致性检查工具，便于三份文档同步维护 |

UI 使用示例数据演示交互，所有订单号、金额、客服名和指标均只用于设计展示。浏览器确认按钮不会创建真实订单；原型只展示状态变化，不能代替 A01 至 A15 的业务验收。

需求是范围依据；本包的页面、字段和接口是设计选择。标为 D01 至 D08 的业务细节或运行参数建议，不自动升级为新需求。评审中可调整实现，仍需维持需求边界和对应验收结果。
""")
    ui=["# 前端 UI 设计","",
      "设计 v0.1，需求基线 1.1。买家端以 H5 验收，管理端以桌面浏览器验收。原型入口为 ui/index.html；顶部灰色场景栏属于评审工具，不进入最终商城产品页面。","",
      "## 视觉与布局","",
      "买家端沿用现有商城 #FA436A 粉色主色，背景 #F7F8FA，正文 #303133，成功 #16856B，警示 #A66A12，错误 #C23B47。管理端沿用 #304156 深色导航与 Element Plus 的 #409EFF 操作主色。业务危险按钮使用文字说明，颜色不作为唯一状态标识。","",
      "正文 14px、次要说明 12px、页面标题 22px；移动端输入框 16px，点击区域至少 44px。布局使用 4/8px 间距单位，表单行间距 16px。正文行高 1.6。输入、错误和只读状态保持可辨识对比。","",
      "买家聊天：桌面左侧 240px 会话列表，中间主聊天区，右侧仅在需要时显示上下文；最大内容宽 1440px。低于 760px 隐藏常驻历史列表，改为历史按钮及抽屉，聊天和底部输入可用，页面不横向溢出。管理工作台桌面为 260px 队列/弹性聊天/300px 业务栏；低于 1100px 业务栏下移，低于 760px 三栏顺排；桌面是主验收环境。","",
      "弹窗最大宽度 560px，移动端边距 16px。包含标题、明确结果描述、取消与主要动作；Esc 可关闭可取消弹窗，Tab 聚焦循环，关闭回到触发按钮。执行中的业务不提供假装回滚的关闭动作。所有表单有可见 label，错误关联字段，通知用 aria-live，图标按钮有名称。","",
      "## 页面与路由","", "| 页面 | 原型入口 | 对应需求 | 主要内容 |","|---|---|---|---|"]
    for pid,title,route,rr,detail in PAGES:
        ui.append("| "+pid+" "+title+" | ui/index.html#"+route+" | "+rr+" | "+detail+" |")
    ui += ["","B01/B02/B03 共用聊天画布，以入口、空会话、商品选择弹层区分。B04、B05 独立路由。最终接入建议：买家新增 pages/agent/chat、pages/agent/checkout、pages/agent/orders；后台新增 /agent/service、/agent/knowledge、/agent/models、/agent/governance、/agent/operations。已有商品、地址和支付页继续承接原业务。","",
      "## 买家交互细节","",
      "| 操作 | 正常流程 | 异常或限制 |","|---|---|---|",
      "| 进入客服 | 只传 product_id 或 order_id，服务端核验后显示上下文卡 | 未登录跳登录；他人订单入口不显示详情；对象失效说明原因 |",
      "| 发消息 | 本地生成 UUID，发送后显示接收状态；服务端确认后开始流式文本 | 网络失败保留草稿；同会话生成中发送按钮切停止，不在前端创建第二任务 |",
      "| 停止及重试 | 保留已输出文本，停止后不展示后续 delta；重试产生新 run，旧业务操作只查询 | 不承诺供应商已停止计费；旧卡片不通过重新生成再执行 |",
      "| 历史与删除 | 历史按更新时间排序；重命名；删除二次确认 | 删除后会话立即不可读取，售后凭证独立保护；清理含模型记忆 |",
      "| 商品比较与 SKU | 展示名称、价格、已有规格；选择 SKU、数量，点击准备加车 | 缺失参数显示未提供；下架/库存不足禁止确认；不能忽略预算 |",
      "| 下单 | 已有地址、优惠、积分 → 重新报价 → 完整确认卡片 → 用户点击确认 | 改参数生成新卡；报价变化、过期、库存不足都失效；成功后只给手动模拟支付入口 |",
      "| 取消订单 | 展示明确订单号、金额、当前状态，点击确认 | 已支付/发货不允许；处理中显示核对，不重复释放库存 |",
      "| 退货 | 定位 order_item_id、选择数量/原因/说明/凭证 → 规则核验 → 确认卡 | 7天外/超数量/历史歧义给原因或人工；AI 不批准也不执行退款 |",
      "| 转人工 | 保存请求并显示等待；用户可选择继续 AI 或撤销等待 | 无人时不显示假预计时间；接管后人工身份可见，旧卡片失效 |",
      "| 图片 | 文件选择 → 本地预览/移除 → 发送 → 状态确认 | 类型、大小和归属校验；识别仅为线索；作为凭证需另建保护引用 |",
      "| 语音 | 开始/取消/时长 → 转写文本 → 用户编辑 → 手动发送；回答主动播放/停止 | 转写不自动确认业务；权限拒绝/服务异常保留文字入口 |",
      "| 反馈与来源 | 对可见 AI 回答赞/踩并填写理由；点击查看活动来源 | 来源停用显示失效；不展示内部工具参数或思考 |","",
      "## 会话与操作状态文案","",
      "| 状态 | 展示及允许操作 |","|---|---|",
      "| AI_ACTIVE | AI 客服；正常发消息，可申请人工 |",
      "| WAITING_HUMAN | 正在等待人工接待；撤销等待、显式开关继续 AI，不承诺时间 |",
      "| HUMAN_ACTIVE | 客服姓名及在线状态；人工消息；AI 不自动回复 |",
      "| CLOSED | 本次服务已结束；可读保留历史，按钮为发起新会话，旧会话禁止发送及确认 |",
      "| QUEUED / RUNNING | 已接收，等待生成 / 正在回复；提供停止 |",
      "| STOPPED / INTERRUPTED / FAILED | 已停止 / 服务中断 / 生成失败；保留部分文本，用户决定重试 |",
      "| PENDING_CONFIRMATION | 展示固定参数、总额、有效期、确认和取消 |",
      "| EXECUTING / UNKNOWN | 正在处理 / 正在核对业务结果；只可查询，不显示再次提交按钮 |",
      "| SUCCEEDED | 展示真实业务结果及现有业务页入口 |",
      "| FAILED / EXPIRED / STALE | 原因明确，旧按钮禁用，选择重新准备 |","",
      "## 人工工作台","",
      "等待队列按请求时间排序。点击领取必须使用最新版本；冲突显示实际接待人，不能在前端先宣称领取成功。我的会话展示未读和客户最后消息。客服显示在线/忙碌/离线，容量作为独立配置；掉线只显示离线，不静默制造新请求。","",
      "会话中部显示买家可见消息；右侧业务上下文仅展示授权摘要，另一个独立备注面板只对员工可见。发送商品/订单卡片时只选业务 ID，由服务端重新取值并核对订单属于该买家。不能代持或伪造买家令牌。","",
      "转交弹窗选择当前在线且有容量的客服，填写原因；关闭弹窗填写结束原因。历史全量访问使用 agent:conversation:history:all，普通接待权限只访问分配范围。权限不足的菜单隐藏，服务端仍必须拒绝直接请求。","",
      "## 知识、模型与治理","",
      "知识列表显示标题、分类、范围、活动版本、草稿处理状态和更新时间。编辑生成新版本；解析结果先预览，可对待构建文本校正。构建失败保留当前活动版本；只有 READY 且质检通过的新版本可发布。停用立即影响新回答，旧答案保留失效提示。","",
      "模型配置按对话、向量、重排、视觉、ASR、TTS 分开。页面显示供应商、模型名、端点注册名、秘密是否配置、启用/活动状态和版本；不回显原始密钥或 secret_ref。提示词管理包含草稿、发布记录和恢复旧版本。用量标注示例、未知、估算，不能把未报告计量当成 0。","",
      "留存配置修改先试算展示文字、图片、音频、检查点和受保护凭证的影响；缩短期限后才能按该试算批次确认执行。清理进度与失败重试单独显示。备份页核对已登记隔离恢复任务，不提供任意命令或服务器路径输入。","",
      "A05 复用现有订单业务详情并增添模拟轨迹；每个节点显示发生时间、地点、说明、模拟标志，编辑需原因。售后分开显示申请、批准、收到货、模拟退款完成；不同按钮分别校验权限和当前状态。","",
      "## 组件与契约对应","",
      "ChatMessage 对应 Message/MessageBlock；ActionCard 对应 Action/Quote/BusinessResult；ConversationHeader 对应 Conversation；KnowledgeVersionPanel 对应 KBDocument/KBVersion/Job；PrivateAttachment 对应 Attachment；所有状态从接口返回，不能通过回答中的关键词推断业务成功。","",
      "## 原型可演示范围","",
      "可演示聊天发送/停止/重试、会话历史管理、商品规格和加车确认、下单报价与异常、人工排队/接管/备注/转交/关闭、知识预览/构建/发布/停用、模型配置及提示词、留存试算、物流维护、售后逐阶段处理。顶部场景选择可切换断线、生成失败、报价变化、过期等状态。媒体使用本地文件预览及转写/播报状态演示；没有请求麦克风权限或调用供应商。","",
      "## 前端接入检查","",
      "普通商城请求保持原超时；AI 流使用独立支持 Authorization 的 fetch/SSE 客户端。读取快照得到 event_cursor，再订阅后续事件；断线按持久游标补传，收到 410 则全量同步。每次事件先校验会话版本和 run_id，停止或人工接管后的旧 delta 不再进入画布。安全渲染 Markdown，禁止任意 HTML 和危险链接；附件通过鉴权字节生成本地 URL，销毁时回收。"]
    write("01-前端UI设计.md","\n".join(ui))

    api=["# 接口设计","",
      "设计 v0.1，OpenAPI 3.1 完整定义见 api/openapi.json。共 "+str(len(ops))+" 个方法与路径组合，包括买家、员工、Java 会员桥接和 Java 员工桥接。接口尚未实现，本次没有发起付费模型调用。","",
      "## 调用与鉴权","",
      "买家和员工浏览器使用同源 /mall-agent/v1/member 与 /mall-agent/v1/admin。Python 通过 Java 的 /mall-portal/agent-bridge/v1 和 /mall-admin/agent-bridge/v1 验证身份及业务事实。管理浏览器只调用 admin 门面；桥接签名只由服务端生成。模型只调用查询和准备工具，不取得 confirm、员工审批或付款工具。","",
      "Authorization: Bearer <当前主体 Sa-Token>。主体 ID 由 Java 返回，禁止相信 x-user-id、前端 member_id 或模型参数。会员与员工登录域分开。每次敏感写请求重新认证；SSE 建连及每30秒复核身份，注销或封禁后断开。员工权限和分配关系逐请求检查；拥有订阅权限不代表可读所有事件。","",
      "桥接同时需要主体令牌和 X-Agent-Signature。服务端签名建议 HMAC-SHA256，签名文本依次为 method、原始路径与规范 query、body SHA256、X-Agent-Timestamp、X-Agent-Nonce，以换行连接。时间允许偏差60秒，nonce保留5分钟防重放；服务身份密钥以受限配置注入，不能交给浏览器/模型。重试重新签名并保留原 operation_no。反向代理必须移除外部伪造的内部身份头。","",
      "客服代查使用当前员工令牌加短时 access_grant，绑定员工、会话、会员、用途和有效期；Java 独立校验权限和 grant，不能接受任意会员 ID 代查。普通员工、知识运营、客服和系统管理员分别授权；Java 网关新增资源必须默认拒绝未匹配规则。","",
      "## 数据格式与通用行为","",
      "- JSON UTF-8，snake_case。Java ID 与事件/消息大序号为字符串；自有对象为 UUID。", "- 成功格式 code=OK、message、data、request_id；业务失败使用真实 HTTP 状态及稳定字符串错误码。Java 旧接口 HTTP200 内部业务码必须在适配器中检查后转换。", "- 日期为带时区 RFC3339；金额非负两位小数字符串，货币 CNY。数量为整数，报价前后所有金额在 Java 精确计算。", "- 所有逻辑写操作带 Idempotency-Key UUID；服务端以主体、接口作用域和键去重，同键不同 body 哈希为409。消息还校验 client_message_id。重放前重新鉴权。", "- 列表使用 next_cursor / has_more；默认20，上限100。会话按 updated_at,id，历史按 seq，排队按 created_at,id。游标签名绑定主体、筛选及排序，不作为授权凭证。", "- 可变对象携带 version，修改请求传 expected_version。不匹配返回409及最新版本，不能默默覆盖。并发领取失败返回实际归属或重新读取提示。", "- 二进制附件响应 Cache-Control: private,no-store；不把令牌放 query、日志或外部来源链接中。普通图片10MB，每消息最多5张；知识20MB；录音60秒并建议10MB字节上限。", "- 读取删除/不可访问对象统一404，避免泄漏存在性；已授权但事件游标过期返回410。来源曾被授权引用而现已停用时仅返回来源失效。","",
      "## 错误及前端处理","",
      "| HTTP | 错误码示例 | 行为 |","|---|---|---|",
      "| 401 | AUTH_REQUIRED / AUTH_EXPIRED | 终止流与新操作，引导原登录，恢复后重新读取状态 |",
      "| 403 | PERMISSION_DENIED | 不展示敏感内容；不自动提升角色 |",
      "| 404 | RESOURCE_NOT_FOUND | 不区分不存在和越权对象 |",
      "| 409 | VERSION_CONFLICT / RUN_ALREADY_ACTIVE / IDEMPOTENCY_CONFLICT | 读取最新状态；不重放新业务编号 |",
      "| 409 | ACTION_EXPIRED / ACTION_STALE / QUOTE_CHANGED / STOCK_INSUFFICIENT | 原卡失效，说明原因，重新准备并再次点击确认 |",
      "| 410 | EVENT_CURSOR_EXPIRED / SOURCE_UNAVAILABLE | 分别重新同步快照，或标记来源失效 |",
      "| 413 / 415 / 422 | FILE_TOO_LARGE / MEDIA_UNSUPPORTED / VALIDATION_FAILED | 字段/文件错误就地提示 |",
      "| 429 | RATE_LIMITED / QUOTA_EXCEEDED | Retry-After；保留消息和人工入口 |",
      "| 503 | PROVIDER_UNAVAILABLE / DEPENDENCY_UNAVAILABLE | 查询有限退避；写结果未知转核对，不直接重下单 |",
      "| 500 | INTERNAL_ERROR | 展示 request_id；不返回异常堆栈或秘密 |","",
      "## 业务准备与确认","",
      "四类参数由 ActionParameters 的 oneOf 约束，不能传任意工具名、URL或SQL。CREATE_ORDER 中 DIRECT 使用已选商品/SKU/数量，CART 使用已有 cart_item_ids，两者互斥；DIRECT 报价与创建不修改买家原有购物车。查询地址、优惠券、积分后，Java 返回 Quote，包括逐行金额、优惠分项、地址摘要、规则版本、参数哈希和5分钟有效期。","",
      "前端准备操作返回 Action，只有用户点击 /actions/{id}/confirm 并携带版本才把状态推进 EXECUTING。确认请求不接收价格、数量等可变参数。任何编辑都必须产生新的 action_id，旧卡标 STALE。action_id 与 Java operation_no 相同；确认服务签发与真实点击绑定的短时确认凭据，Java 再核对主体、哈希、期限与报价。","",
      "Java 最终操作结果 SUCCEEDED/REJECTED 与业务同事务提交。Python 超时标 UNKNOWN；GET 原编号出现 NOT_COMMITTED 仍保持 UNKNOWN，因为原事务可能在途。只有核对清楚才允许重新准备；禁止通过重试回答、刷新或检查点恢复重新创建操作。已成功操作即使卡片随后过期也返回原成功结果。","",
      "## SSE 事件","",
      "格式为标准 id/event/data 帧，id 为全局单调字符串，允许间隙。data 的完整 JSON Schema 见 sse-events.schema.json；payload 不含内部思考、未筛选参数或他人数据。注释心跳每15秒，不携带业务 ID，也不推进游标。快照与高水位必须一致性读取，重连优先取状态再补传事件。","",
      FENCE+"text",
      "id: 1208",
      "event: message.delta",
      'data: {"type":"message.delta","event_id":"1208","conversation_id":"5a0f2d23-88cb-4d0f-a431-771fdcb35531","conversation_version":3,"occurred_at":"2026-11-10T12:00:00Z","payload":{"message_id":"fb5e0f15-8319-4151-98d9-e20c953a11e2","run_id":"267d5742-b54a-4353-806d-9055cab9d60f","offset":0,"text":"为你找到以下商品。"}}',
      "",FENCE,"",
      "| 事件 | payload 作用 | 受众 |","|---|---|---|"]
    for name in json.loads((ROOT/"api/sse-events.schema.json").read_text(encoding="utf-8"))["oneOf"]:
        n=name["properties"]["type"]["const"]
        api.append("| "+n+" | "+("只允许内部备注" if n=="note.created" else "见 Schema 中该事件的 typed payload")+" | "+("STAFF_ONLY" if n=="note.created" else "管理权限过滤" if n=="job.updated" else "会话所属买家及已授权员工")+" |")
    api += ["","message.delta 的 offset 是目标文本的字符偏移，前端仅追加符合当前 offset 的数据；重复事件以 event_id 去重，offset不一致则读取最终消息。message.completed 以数据库完整消息覆盖临时增量。停止、人工接管、会话版本变更使旧 run 后续事件不可展示。","",
      "## 接口目录","", "| operationId | 方法与路径 | 请求 → 返回 | 权限 | 需求 / 页面 |","|---|---|---|---|---|"]
    for x in ops:
        api.append("| "+x["id"]+" | "+x["method"]+" "+x["path"]+" | "+(x["body"] or "路径/查询")+" → "+x["result"]+" ("+str(x["status"])+") | "+(x["permission"] or "当前主体及对象归属")+" | "+" ".join(x["requirements"])+" / "+" ".join(x["ui"])+" |")
    api += ["","## 权限矩阵","",
      "| 角色建议 | 权限范围 | 数据范围 |","|---|---|---|",
      "| 买家 | member接口及四类本人确认 | 仅本人会话、订单、附件与操作 |",
      "| 客服 | conversation:read/reply/note、handoff:claim/close、presence:write、staff:read、business:read、events:subscribe、attachment:upload/read | 当前分配会话；等待列表只显示接待必要摘要 |",
      "| 客服主管 | 客服权限加 handoff:transfer、conversation:history:all、staff:manage | 主管获授权的全量接待历史；受限资料访问留审计 |",
      "| 知识运营 | kb:read/edit；发布者另授 kb:publish；job:read/retry、attachment:upload/read | 对应知识资料与任务；不自动获得私聊或订单访问 |",
      "| 系统管理员 | model、prompt、settings、statistics、audit、retention、backup 各项权限分别授予 | 不因系统配置权限自动获取全部客户私聊 |",
      "| 商城售后/物流员工 | logistics:read/write、return:read/review/receive/refund | Java 再核验业务资源权限；AI 无这些权限 |","",
      "上述简写统一前缀 agent:。系统管理员不使用万能角色绕过对象权限。内部备注、秘密配置引用和原始凭证逐用途授权；全量历史查询与当前分配权限明确分离。","",
      "## 集成验收前置","",
      "接口进入实现后，用 OpenAPI 生成或校验客户端，核验两名买家隔离、员工角色差异、非法状态、幂等哈希冲突、并发领取、报价变化、跨服务超时、事件补传和附件保护。文档及 schema 静态通过不代表接口已运行；供应商 Responses/工具调用/结构化输出/媒体兼容性仍需用独立服务凭据验证。"]
    write("03-接口设计.md","\n".join(api))

    trace=["# 需求到设计的映射","",
      "以下需求、优先级、场景和验收编号直接来自需求基线1.1。页面编号、表和 operationId 是设计承接，不改变原需求范围。P0/P1/P2 全部属于完整交付。","",
      "| 需求 | 原需求及优先级 | UI | PostgreSQL / MySQL | operationId | 原场景与验收 |","|---|---|---|---|---|---|"]
    sqlmaps={"R04":["pms_product（沿用）","pms_sku_stock（沿用）","pms_product_attribute_value（沿用）"],
      "R08":["oms_cart_item（沿用）","oms_agent_quote","oms_agent_operation"],"R09":["oms_order（沿用）","oms_order_item（沿用）","oms_agent_quote","oms_agent_operation"],
      "R10":["oms_order（沿用）","oms_order_item（沿用）","oms_shipment","oms_shipment_event"],"R11":["oms_agent_operation"],
      "R12":["oms_order_return_apply","oms_after_sale_rule_version","oms_return_proof_reference","oms_agent_operation"],
      "R18":["oms_agent_operation","oms_agent_audit"]}
    for r in reqs:
        rid=r["id"]
        pp=[p[0] for p in PAGES if rid in p[3].split()]
        tt=[t["name"] for t in tables if rid in t["requirements"]]+sqlmaps.get(rid,[])
        oo=[x["id"] for x in ops if rid in x["requirements"]]
        trace.append("| "+rid+" | "+r["priority"]+" "+r["scope"]+" | "+" ".join(pp)+" | "+"、".join(tt)+" | "+"、".join(oo)+" | "+r["mapping"]+" |")
    trace += ["","## 设计完整性核对","",
      "检查 R01 至 R18 均有页面、数据和接口。B01/B02/B03 共用聊天原型，以入口状态和弹层承接；其他页面独立切换。A01 至 A15 的正常、异常及权限要求在接口和状态规则中承接，具体测试用例在实现阶段编写。","",
      "本次原型只供产品交互评审；数据库 DDL 未执行；OpenAPI 描述尚未实现的契约。不得把设计覆盖率记作功能验收通过。"]
    write("04-需求设计映射.md","\n".join(trace))
    write("05-设计决策与环境.md","""
# 设计决策与环境

设计 v0.1，2026-11-10。需求基线 1.1 保持原样。本文件区分需求已确定的约束、设计选择和仍需在实现前确认的业务口径。

## 已确定约束

单商家自营；保留 Java 商城、现有买家和管理前端；Python FastAPI/LangGraph 独立服务；新增客服数据使用 PostgreSQL；Milvus 作为派生索引。商城/Python 本机运行、中间件云端运行。H5 优先验收；模拟支付与物流；人工审核和模拟退款；本人数据隔离；写入需要点击确认与 Java 事务保障。完整交付包含 P0/P1/P2。

## 设计决策

| 编号 | 决策或建议 | 属性与影响 |
|---|---|---|
| D01 | DIRECT 聊天下单不暗中增删现有购物车；与 CART 路径共用报价及创建计算器 | 内部设计选择，保护用户原购物车 |
| D02 | action_id 与 Java operation_no 使用同一个 UUID；确认后参数不可变 | 内部设计选择；未知结果只查原编号 |
| D03 | 收货后7天建议按 receive_time 起168小时，截止时刻允许提交；无可靠收货时间或争议转人工 | 业务口径建议，实施前确认；不改原7天需求 |
| D04 | 实付优惠按分分摊，最大余数法，余数相同按明细ID稳定排序；部分退货按购买件序号分配分值，累计不超原行实付 | 金额口径建议，实施前用多数量/多优惠样例确认；历史无法对平转人工 |
| D05 | 客服容量默认每人3会话；20秒心跳，60秒过期；离线保留归属，由主管转交 | 运行参数建议，后台可配置；不新增自动抢单/自动转交需求 |
| D06 | 持久事件补传建议7天；普通写请求幂等引用至少24小时；业务操作结果不随聊天清理 | 运行参数建议；超游标窗口重新同步 |
| D07 | 原售后status保持兼容，新增process_stage区分申请/批准/收到货/模拟退款完成；历史阶段为空则明确展示旧状态 | 数据兼容选择，不虚构历史细节 |
| D08 | 知识文件建议200页上限，录音建议10MB字节上限，文字10000字；保留期遵循需求默认90/7天 | 建议限额，后台参数受控；未经支持不自动OCR扫描PDF |

以上建议不作为用户已确认的新产品要求；设计文档和原型均标识相应默认值。它们不妨碍评审，实施前需固定口径，必要时通过需求变更记录更新基线。

## 本机 MySQL 检查

已确认程序路径 D:/develop/mysql-8.0.35-winx64/bin/mysql.exe 与 mysqld.exe，版本均为8.0.35 Win64。检查时未发现 MySQL/MariaDB Windows 服务、mysqld进程或3306/3307/13306监听；该解压目录下未见data或ini。程序不在当前会话PATH中。

这说明本机已有客户端和服务端二进制；没有据此认定本机已有可用数据库实例。本次没有初始化data、注册服务、启动mysqld、导入SQL或创建数据库。现有商城仍使用云端MySQL8.0.35。本机MySQL的存在不改变客服库使用PostgreSQL的既定需求。

## 已核查环境与兼容工作

此前只读扫描：云端7类中间件均运行，8核、约15.1GiB内存、可用约11GiB、磁盘剩余约29GiB。该信息是采样，不保证PostgreSQL/Milvus新增后的共存容量。当前设计不部署容器、不安装数据库。

当前工作区已有业务修复及未提交改动；原README的未改业务代码/正在运行描述与实际不完全一致。设计只新增docs内容，保留原业务文件及修改。下一阶段需要对准确报价、事务幂等、售后明细与时间规则、商品可见性、鉴权桥接进行实现或复验。

## 参考项目的具体借鉴

| 参考 | 已核对内容 | 本设计如何使用 |
|---|---|---|
| D:/智能体参考项目/天机/prd/天机agent V0.3.zip | 历史对话、准备回复/回复中、停止、语音、回复反馈及版本交互 | 借鉴聊天布局与交互状态；不纳入课程社区、通用写作等商城需求以外功能 |
| D:/智能体参考项目/天机/代码/tjxt.zip 内 tj-aigc | 路由智能体、预下单工具、事件VO、会话与音频服务 | 借鉴职责划分、业务卡片与预下单思路；自定义typed事件、鉴权、幂等和持久补传 |
| D:/智能体参考项目/资料(1)/07.项目源码.rar 内 agent-service | FastAPI模块分层、LangGraph状态、PostgreSQL检查点、父子切片 | 借鉴编排、会话隔离的意图和父子检索；保留本项目Java事实来源 |

Python参考的人工作为提示文字节点，不含真实接待系统；接口直接从x-user-id取身份，需要替换为可信认证。其知识管线先删除旧切片的做法不符合本需求的版本发布，本设计改为新版本构建成功后切换。天机查询/清理记忆等源码需逐入口补齐归属检查。两个代码包均未发现明确许可证文件，本包不复制其业务源码。

## 评审与实现界线

本次交付三项设计及追踪关系。静态检查覆盖OpenAPI引用、operationId、页面与需求映射、DDL表/索引/FK引用、原型脚本和内部导航。没有运行商城测试、调用模型或执行数据库迁移。

评审后可分别按页面交互、数据约束与接口契约提出调整；完成设计评审后再进入实现、隔离库迁移验证和供应商接入验收。数据库字段、接口路径或页面布局的普通调整可维护设计版本；涉及功能范围、业务结果或验收标准的变化须记录为需求变更。
""")
    (ROOT/"ui").mkdir(exist_ok=True)
    (ROOT/"ui/page-catalog.json").write_text(json.dumps([dict(zip(["id","title","route","requirements","description"],p)) for p in PAGES],ensure_ascii=False,indent=2),encoding="utf-8")
    (ROOT/"api/requirement-catalog.json").write_text(json.dumps(reqs,ensure_ascii=False,indent=2),encoding="utf-8")
    from architecture_contracts import extend_documents
    extend_documents(ROOT)
    return len(PAGES)

if __name__=="__main__": print("UI pages:",generate())
