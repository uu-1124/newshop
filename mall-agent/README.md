# 商城智能体开发与运行

2026-11-14核心功能收尾见[47](../docs/47-核心媒体反馈与售后规则收尾验收.md)：H5录音与可编辑转写、本人回答分段播报/停止/缓存、反馈读写、人工模式买家图片、不可变售后规则发布已接线。语音请求独立持久归属与额度，新增PG迁移0011；生产ASR/TTS及后台同参数V2发布已验。多订单改选普通7轮和订单入口2轮真实顺序通过；支付退款保持演示。设备麦克风实际采集受浏览器授权限制，验收范围与完整证据见47号。

2026-11-14购物车与结算后续见[45](../docs/45-购物车选择与多轮结算改选验收.md)：会员购物车行选择独立校验、有限自然修改、当前购物车变化重读，修原商品不变/换券表达及Java结算当前名称规格。最终237项Python、46项Java通过；真实直购5轮/购物车4轮完整顺序通过，原4轮失败保留，新增70笔全部归属。无确认或订单写，临时数据清理、公共策略保留；完整目标仍active。

2026-11-14确认卡后续见[44](../docs/44-四类确认卡时效与人工接管验收.md)：补锁等待期间到期、准备超期事务回滚、历史V1取消摘要再次校验和前端旧回调保护。30项时效、41项扩大、8项领取竞争、54项前端分别通过，类型/H5构建通过；13个实际页面场景及网关拒绝已验。支付退款保持演示，完整十组及全项目仍未交付。

2026-11-14辅助调用后续见[42](../docs/42-辅助模型调用归属与中断记账验收.md)：可信任务上下文、run/job/主体关联、请求前UNKNOWN起始行、失效预留回滚和完成/对账保护已补，检索协议错误在成功落账前校验。48项主体、21项知识事实记忆/进程退出、最终34项关联协议分别通过；真实知识4调用和后台1调用正确归属、总账505、历史未知保留。智能体网关拒绝的HTTP状态/封套另修，15项测试和实际401/403通过。完整目标仍active。

2026-11-14会员选择后续见[41](../docs/41-会员商品选择与自然改选验收.md)：ADD_CART/DIRECT增加图与代理独立会员商品/SKU/数量校验，自然规格/中文数量/有界改选及缺参澄清；补读取规格、顿号字段串扰和暂停兜底样本修正。144项准备组合、最终140项选择/结算分别通过；真实模型10轮中8轮验收通过、2轮失败保留，41对话+2辅助调用、无确认/dispatch。四会话清理、原业务/策略不变；完整目标仍active。

2026-11-14确认与失败恢复见[39](../docs/39-确认竞争与失败回复恢复验收.md)：四类确认竞争8项真实锁等待验证、消息可选reply_failure、额度窗口/安全说明及新问题后旧重试服务端拒绝。后端38项及前批39项分别通过、前端33项/类型/H5构建通过；真实额度拒绝与人工降级完成，无新增供应商调用，原业务不变，完整目标仍active。

2026-11-14多轮准备后续见[38](../docs/38-多轮选择与四类确认卡更新验收.md)：否定券/积分和明确修改解析、新消息使未确认旧卡失效、prepare重放当前状态已修。最终51项Python、31项前端及类型/H5构建通过；7轮真实模型准备成功，1轮每日额度失败保留，不升额不清用量。四卡UI/旧卡拒绝及清理通过，无业务执行，完整十组仍active。

2026-11-14流式控制后续见[37](../docs/37-流式控制失败恢复与重连页面验收.md)：修复控制失败后正文冻结、停止历史重开为空及等待AI控制，27项前端回归、类型/H5构建和实际页面/SSE补传通过。验收使用明确合成writer，未调用模型；四个新会话均清理，原业务和策略不变。完整十组及性能/故障剩余继续active。

2026-11-14留存并发后续见[35](../docs/35-留存清理与售后保护并发验收.md)：锁后重读新保护、退货凭证当前期限/锁序、删除重试和旧生成/checkpoint/人工并发已验；真实网关保护保留及释放后的物理回收通过。部署增量`deployment/retention-races-manifest.json`，完整十组目标仍active。

最新状态（2026-11-14）：20模块、12图节点、130设计操作（Python104/Java26）、101实际Python操作，PG迁移0010。核心咨询、知识/人工、演示交易、治理/统计/留存、周期备份/隔离恢复、监控及规定持续容量已有实现与阶段证据；语音/反馈、人工模式买家图片、售后规则版本、性能及完整故障/灾难等仍欠，见[32全量核查](../docs/32-智能体模块全量完成度核查.md)。最新物流与取消卡页面见[33](../docs/33-物流时间与取消卡页面验收.md)，凭据拒绝/人工续签、outbox租约及进程崩溃恢复见[34](../docs/34-异常核对与Outbox崩溃恢复验收.md)。整体仍为PARTIAL，支付退款物流保持演示；部署增量见`deployment/recovery-boundaries-manifest.json`。下文早期版本和状态保留为历史背景，不能据此否定后续已经落地的能力。

初始阶段运行版本 `order-0.9`，2026-11-11；下列旧阶段链接保留作历史记录。现有 Java 商城继续负责会员、商品和交易；本工程负责独立的会话、消息、run 与事件。已运行 GPT 文字/图片咨询和只读商品工具，并补齐全模块结构登记。总体设计见 [07](../docs/07-系统架构与模块设计.md)，最新报价与下单闭环见 [17](../docs/17-复用Java计价与订单创建闭环验收.md)，加购确认闭环见 [16](../docs/16-加购确认与结果核对闭环验收.md)，人工接待闭环见 [15](../docs/15-人工接待闭环实施与验收.md)，知识闭环见 [14](../docs/14-知识管理与引用闭环验收.md)，运行基础验收见 [13](../docs/13-运行基础实施与验收.md)，结构清单见 [12](../docs/12-整体工程框架与模块落地清单.md)，图片验收见 [11](../docs/11-GPT图片咨询闭环验收.md)。

## 当前能运行什么

真实会员登录 → Java Gateway → Python 身份复核 → PostgreSQL 保存消息与 run → 独立 worker 领取全局名额 → GPT 选择只读商品工具 → 经签名桥接读取真实上架商品 → GPT 续写回答 → LangGraph 保存检查点 → 持久化分段事件 → H5 展示并恢复历史。

本机 `.env` 选择 `openai_responses`，对话与图片理解使用 `gpt-6.1-sol`。`fixture`用于确定性测试。H5支持最多2张、每张5MiB的私有图片上传、识别与追问。阿里云辅助适配器及Milvus此前已验证。知识上传/校对/索引/发布/回滚/引用、人工文字图片接待及卡片/SSE、模型提示词后台、加购、CART/DIRECT确认下单、订单取消及售后演示已激活。N07/N08可查询当前会员订单及售后，模型没有执行交易或退款的工具。语音交互业务尚未完成。

## 工程边界

```text
app/
  api/                 HTTP DTO、会员鉴权、已激活契约、SSE
  application/         会话/消息命令、幂等、run 状态机和租约
  domain/              Principal、领域错误、外部能力协议
  infrastructure/      PG事务/事件、Java桥接、GPT/阿里云适配器、模型账本、Milvus候选、fixture
  agents/              LangGraph图及持久化检查点
  workers/             生成进程、过期租约恢复进程
  bootstrap.py         依赖组装
  framework.py         20模块与122设计操作责任检查，结构状态和业务状态分开
  main.py              生命周期、探针、内部受控查询代理
  module_catalog.py    M01–M20模块状态与代码责任
migrations/            Alembic版本和冻结的SQL基线
scripts/               迁移、真实网关冒烟验证
tests/                 单元和隔离PostgreSQL集成测试
```

完整模块的类型端口在 `domain/capabilities.py`，延后业务装配在 `application/extensions.py`。`agents/workflow.py` 为当前运行的完整 12 节点图，`live_workflow.py` 绑定实际租约、上下文、GPT 路由、只读商品子图与持久输出。尚未实现的专项业务节点明确拒绝。`workers/jobs.py` / `outbox.py` 已运行真实消费/重试及通知发布循环。结构检查执行 `python scripts/check_framework.py`；两端完整契约类型由 `scripts/generate_web_contracts.py` 生成。内部状态清单为 `/internal/framework`，不经网关公开。

API、generation、scheduler、jobs、outbox 五类进程共享领域代码，通过数据库协调，不共享进程内队列。`DialogProvider` 已接入图；`PrivateObjects` 已用于聊天图片。Milvus 提供候选适配器，知识检索回 PG 复核权限/版本，N05 生成带引用答复，N12 原子提交；RabbitMQ/Redis 通知已运行，PG 是任务与事件的事实来源。M01–M20 是逻辑责任，不拆成 20 个微服务；状态可在模块目录核对。

Python 应用运行时不直连商城 MySQL；专项验收脚本会读取业务事实并维护专用测试买家。会员令牌只放在 API 内存；生成 worker 凭内部密钥和当前租约请求受控商品查询，拿不到令牌。图检查点含问题、历史、有界图片观察、商品摘要和回复，不含原始图片、密钥或回调。API 重启后旧授权上下文对应的未完成 run 转为 `INTERRUPTED/AUTH_CONTEXT_LOST`，旧 worker 不能继续写入。

## 已激活接口

网关转发 `/mall-agent/v1/member/**` 与需员工登录的 `/mall-agent/v1/admin/**` 到 `127.0.0.1:8099`。内部代理、探针与文档不经此路由公开。当前激活88个Python操作，完整设计122个操作。下表保留初始11个买家基础接口，完整清单以运行时 `/internal/openapi.json` 为准；操作数不能换算为整个蓝图的完成比例。

| 方法 | 路径（省略 `/mall-agent/v1/member`） | operationId |
|---|---|---|
| GET | `/me` | memberMe |
| POST / GET | `/conversations` | createConversation / listConversations |
| GET | `/conversations/{conversation_id}` | getConversation |
| POST / GET | `/conversations/{conversation_id}/messages` | sendMemberMessage / listMessages |
| GET | `/conversations/{conversation_id}/events` | memberEvents |
| GET | `/runs/{run_id}` | getRun |
| POST | `/runs/{run_id}/stop` | stopRun |
| POST | `/attachments` | uploadMemberAttachment |
| GET | `/attachments/{attachment_id}/content` | readMemberAttachment |

员工签名桥接 `/mall-admin/agent-bridge/v1/identity` 使用独立密钥、nonce 防重放，每次从 DB 读取员工状态及启用角色权限；不缓存授权、不隐式授予超级管理员权限。

Java会员桥接包含身份、商品筛选/详情/SKU/分页、订单/物流/退货查询及确认交易。签名绑定方法、路径/查询、请求体摘要、时间戳、nonce和用途，Redis拒绝重放。商品过滤删除/下架数据，订单按当前会员归属读取；员工售后桥接每次重新核对权限。

## 本机启动

目前采用 Windows 本机运行应用、云端 Docker 运行中间件。PostgreSQL 与商城 MySQL 隔离，云端回环 15432 通过 SSH 转发。Milvus 独立栈同样仅通过回环隧道访问 19530/19091。秘密配置位于被 Git 忽略的 `mall-agent/.env`、`deployment/.run/agent-env.json`、`deployment/.run/milvus-env.json`、`deployment/.run/objects-env.json`、`deployment/.run/runtime-env.json` 和 `deployment/.dev-ssh.json`，不得提交。模型配置模板为 `.env.example`；已有本机环境已配置。

首次准备（当前机器已完成，不需反复执行）：

```powershell
Set-Location D:\newshop\mall-agent
& D:\newshop\deployment\.run\agent-tools\bin\uv.exe sync --python 3.13 --locked
Set-Location D:\newshop
& D:\develop\nodejs\node.exe deployment/prepare-agent.cjs
& D:\develop\nodejs\node.exe deployment/prepare-agent-runtime.cjs
```

Java 桥接或网关变更后，先构建再停服切包；构建失败可保留原运行实例：

```powershell
$env:JAVA_HOME = 'D:\develop\jdk-17'
Set-Location D:\newshop\mall-swarm
& D:\develop\maven\bin\mvn.cmd '-Dmaven.repo.local=D:\develop\cache\maven' '-Ddocker.skip=true' '-DskipTests' -pl mall-portal,mall-gateway,mall-admin -am package
if ($LASTEXITCODE -ne 0) { throw 'Java build failed' }
Set-Location D:\newshop
& .\deployment\stop-local.ps1
& .\deployment\start-agent-demo.ps1
```

`start-agent-demo.ps1` 需要管理器已停止；将三个新包暂存至 `.run/agent-jars`、通过短时 SSH 隧道执行迁移，然后启动现有管理器。普通日常启动使用 `deployment/start-local.ps1`；完整停止使用 `deployment/stop-local.ps1`。启停覆盖受管理的进程树，避免 Python 启动器退出后解释器继续占用端口。只重启某个智能体进程可执行：

```powershell
& D:\newshop\deployment\restart-agent-process.ps1 -Name agent-api
# Name 也可为 agent-generation、agent-scheduler、agent-jobs 或 agent-outbox
```

已有环境完整重建脚本 `rebuild-backend.ps1` 更新基础 jar，启用智能体时也同步备份和刷新 portal/gateway/admin 暂存包。只构建这三个模块时可使用上面的定向构建与切包流程。

访问 H5：`http://127.0.0.1:5173/#/pages/agent/chat`，使用现有会员登录。内部探针：`http://127.0.0.1:8099/livez`、`/readyz`，激活契约：`/internal/openapi.json`。readyz 检查数据库、迁移和名额结构，不代表 Java、worker 或全部中间件健康，必须结合冒烟检查。

## 验证

管理器和数据库隧道运行时：

```powershell
Set-Location D:\newshop\mall-agent
& .\.venv\Scripts\python.exe -m ruff check app scripts tests
& .\.venv\Scripts\python.exe -m ruff format --check app scripts tests
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe scripts/smoke.py
& .\.venv\Scripts\python.exe scripts/smoke_runtime.py
& ..\deployment\restart-agent-process.ps1 -Name agent-api
# 等 readyz 恢复后验证上一轮会话仍在
& .\.venv\Scripts\python.exe scripts/smoke.py --verify-persistence
# 以下两项会产生真实模型调用，仅在需要验收相应适配器时执行
$env:PYTHONPATH='.'
& .\.venv\Scripts\python.exe scripts/verify_models.py
& .\.venv\Scripts\python.exe scripts/verify_vectors.py
# Milvus 针对性重启后，复用已存向量验收，不再调用模型
& .\.venv\Scripts\python.exe scripts/verify_vectors.py --verify-persistence
```

测试使用独立的 `mall_agent_architecture_test` 数据库，保留带 synthetic member ID 的测试记录；不调用真实模型（消息适配器测试使用独立命名的云端队列/频道并清理）、不写商城 MySQL。正常 smoke 在 LIVE 模式会调用 GPT；它使用演示会员，在客服库创建标记会话并读取商品。可通过 `MALL_DEMO_USER` / `MALL_DEMO_PASSWORD` 指定会员，脚本不输出令牌。向量验证使用单独的合成 probe 集合。

提交有序事件由最后取得的 `ag_event_clock` 锁保证；快照使用 REPEATABLE READ。数据库名额限制全局 3 个活动生成任务，会话限制 1 个，旧 fencing token、失效租约和停止世代均阻止追加输出。API 内存授权决定本轮仅支持单 API 进程；横向扩容前必须重新设计共享授权和恢复。

迁移 `0001` 的 29 张领域表基线冻结；`0002` 增加图片观察表；`0003` 增加任务唤醒、worker 心跳表和通知重试时间，0003 时为 32 张领域表及 4 张检查点表；当时迁移0006为33张领域表及4张检查点表；当前迁移0007。迁移不支持破坏性 downgrade。运行基础验收见 13，图片验收见 11，辅助模型和向量验收见 10；通用备份恢复、留存和评测治理仍待完成；会话图片清理及知识容量保护已按文档19验收。


人工接待支持会员申请/取消、员工在线/容量、原子认领、双方文字/图片、内部备注、转接/结束、业务卡片、员工SSE及主管审计。`AGENT_HANDOFFS_ENABLED=true`绑定实际服务。最初0005阶段验收保留于`deployment/handoff-manifest.json`，后续扩展见文档19；该manifest属于历史记录。

本轮只读复核：`python scripts/verify_order_lifecycle_state.py`，按保存的专用证据核对服务、订单/退货数量金额、清理、凭证和运行包，不调用模型。`smoke_order_lifecycle.py --models`调用真实模型且有持久断点状态，已有记录不要清空重跑。


加购历史阶段：迁移 `0006`，`AGENT_ACTIONS_ENABLED=true` 绑定实际 ADD_CART 准备/确认/取消/核对。先运行 `scripts/prepare_actions.py` 应用 MySQL 三表、生成库外用途分离密钥，再部署含 CartGuard 的 Java portal 包；原购物车写也依赖互斥表。首次 execute 仅由实际买家点击发送一次，scheduler 恢复仅核对。验收摘要见 `deployment/cart-manifest.json`，此为加购阶段记录；订单创建现已按下文 order-0.9 开放，此后智能体售后已按文档20开放。

最初CART下单阶段版本 `order-0.9`，当时迁移为 `0006`；以下53/116是该历史阶段数量，当前为88/122。`AGENT_ORDERS_ENABLED=true` 与 Java `MALL_AGENT_ORDERS_ENABLED=true` 开放 CART 结算；先运行 `scripts/prepare_orders.py` 准备 MySQL 报价内部指纹列，再部署 portal 订单适配器并重启 API/generation/scheduler。新增 getCheckoutOptions，Python 激活 53/116 接口；原商城与确认卡复用 Java previewOrder，订单仍调用原业务写入。`scripts/smoke_order.py` 使用专用买家创建测试待付款订单、注入响应丢失并核对原结果，随后通过原接口取消并保留审计。不会付款。详见文档17与 `deployment/order-manifest.json`。

2026-11-12 结算增量 `ORDER_CHECKOUT_CENTS_V2` 已通过金额守恒与真实隔离 MySQL 回归，并已切换至暂存运行包。普通商城下单和智能体共用 Java 计价；尾差拆分新订单明细保存，确认卡按原购物车行汇总，无需新增数据库迁移。真实 V1 旧卡拒绝、新卡下单、重复确认、原编号恢复、普通下单及取消恢复已验证；专用买家的订单 110、111 均已取消，库存和积分恢复，浏览器确认卡至原订单详情已核对。`scripts/smoke_pricing.py` 保留两阶段升级和断点续验流程；已有验收记录不要清空重跑。本轮还修复买家 SSE 类型检查和离线框架 CLI 的实时环境污染；`python scripts/check_framework.py` 无需 PYTHONPATH，也不会读取实时开关。该结算阶段部署记录见 `deployment/pricing-manifest.json`，本轮当前部署以文档20为准；该阶段完整结果见 [18](../docs/18-结算金额修复与质量检查收口.md)。
