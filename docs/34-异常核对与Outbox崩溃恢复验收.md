# 异常核对与Outbox崩溃恢复验收

实施日期：2026-11-14，Asia/Shanghai。承接33号，推进原十组第9组UNKNOWN/Java业务outbox及相关故障恢复。支付、退款、物流保持演示；本轮没有调用模型、创建新订单或执行支付退款。

**本轮完成核对凭据拒绝后的人工状态、原始确认依据校验、过期发布租约防护，以及Python/Java进程实际退出的隔离恢复验证，并已部署及通过真实网关核对验收。** 完整十组目标继续active；不把这批测试称作所有F01–F27或全部恢复组合均已完成。

## 实现改动与原因

| 改动 | 行为与目的 |
|---|---|
| mall-agent/app/application/actions.py | Java核对返回CONFIRMATION_INVALID等明确凭据错误时，action保持UNKNOWN、dispatch转MANUAL_REQUIRED，记录RECONCILE_GRANT_REJECTED并清除租约；普通网络/Java不可用仍有限退避，不把未知结果当失败，不重放execute |
| mall-agent/app/application/action_recovery.py | 员工续签除校验参数/审计/控制世代外，还核对原审计confirmed_at与action一致、operation_no与action_id一致。依据缺失或不一致时拒绝续签，不签出新的执行权限 |
| mall-swarm/mall-portal/.../OrderBusinessOutbox.java | published/failed回写同时要求租约仍有效。即使新发布者尚未领取，旧持有者过期后也不能改写SENT/PENDING或延长重试窗口；保留原owner/fencing校验 |

原有“首次确认仅发送一次execute，恢复只查原编号/原子封闭”的边界保持。没有新增模型确认工具、业务接口或数据库迁移。

## Python隔离数据库验证

使用mall_agent_architecture_test，单个pytest进程串行运行。外部业务桥接在这层使用明确合成账本，不把它写成真实Java/MySQL交易证明。

| 场景 | 已验证结果 |
|---|---|
| Java明确拒绝核对凭据 | 保持UNKNOWN并进入MANUAL_REQUIRED，后续扫描不继续调用桥接；原execute总计一次 |
| Java传输不可用 | 保持UNKNOWN/RECONCILING，按原规则可继续核对，不错误转业务失败 |
| 本地凭据过期/不能解密 | 转人工状态，不向Java发送无法使用的凭据 |
| 原参数、确认时间、审计时间、operation_no、审计epoch分别改变 | 五类反例均拒绝续签，dispatch完整行不变，未生成新权限 |
| 核对仍持有效租约 | 员工恢复返回ACTION_RECONCILING，不抢占当前有效核对 |
| 旧租约过期、员工续签后旧返回到达 | 新fencing阻止旧返回提交；新凭据保留原操作、会员、类型、hash、epoch、quote、审计和确认时间，purpose仍仅reconcile_only |
| 员工权限已撤销或登录过期 | 实际ASGI HTTP入口在读取幂等缓存前重新鉴权，撤权403、登录过期401，不增加恢复审计或改变dispatch；这层Java身份供应商为合成当前身份 |
| 确认进程在业务前退出 | 独立Python子进程在COMMIT A后直接os._exit(73)，新Services实例及并发核对扫描只封闭原编号，结果FAILED/RECOVERY_NOT_EXECUTED，不重放execute |
| 确认进程在合成业务提交后退出 | 合成外部账本先fsync，子进程直接退出；新实例读取原成功结果，SUCCEEDED，合成执行计数仍一次 |

子进程的连接配置只经stdin传递，不写入命令参数、普通日志或合成业务账本。原始令牌不落检查点/结果。测试结束将本次合成未决操作收为测试终态，防止污染后续核对扫描。

第一批action/recovery/order为22项通过、213.94秒；随后增加本地凭据及HTTP撤权/过期和治理/恢复回归，最终扩大批次**32项通过、279.21秒**。两批重叠，不相加为54项。

日志：`deployment/.run/completion-recovery-boundaries-tests-20261114.log`、`completion-recovery-expanded-tests-20261114.log`。新增进程辅助文件为`mall-agent/tests/recovery_process_worker.py`。

## Java业务Outbox与真实MySQL验证

MySQL写入仅限mall_agent_cart_test，测试账号和JDBC目标均校验固定隔离库。本轮没有通过SQL修改实际商城业务行，也没有重启云端MySQL或RabbitMQ。

| 场景 | 已验证结果与范围 |
|---|---|
| 订单与outbox同事务提交/回滚 | 原订单、操作和通知一起提交或回滚；保留既有真实MySQL回归 |
| 租约过期，尚无下一持有者 | 旧published和failed都不能写入，仍保留PUBLISHING及未发布事实 |
| 新实例回收租约 | fencing递增；旧持有者报告不能覆盖新发布状态 |
| SENT但未被消费，模拟通知丢失 | 未到重试时不领取，到期后重新领取同event_key；资源只恢复一次 |
| 消费变更资源后、写消费回执前异常 | 事务回滚订单、库存锁、券、积分及消费标记；新消费者可重试一次并收敛 |
| 发布者实际进程退出 | 独立Java子进程提交claim后Runtime.halt(73)；父进程验证持久租约存在、过期旧回写拒绝及新fence恢复 |
| 消费者实际进程退出 | 子进程在订单/资源变更后、事务提交前Runtime.halt(73)；真实MySQL连接关闭回滚，后继消费及重复消费保持唯一资源恢复 |
| broker确认丢失、mandatory路由退回、连接失败 | 实际OrderOutboxPublisher代码通过模拟AMQP Channel故障验证，不标SENT、回到可重试并关闭连接；不是停掉实际RabbitMQ的演练 |
| 正常发布 | 校验persistent、mandatory、原event_key作为messageId及broker confirm；模拟Channel层验证 |
| 支付后或提前到达的取消通知 | 真实MySQL原回归验证已付款不取消、未到期不取消 |

第一轮**28项实际通过、0跳过**：发布器4、凭据安全4、真实MySQL订单20。随后新增实际Java进程退出，针对相关四个MySQL场景及发布器/凭据重验，**12项通过、0跳过**，其中两个场景确实启动并终止独立Java进程。批次有重叠，不累加。

测试文件：`AgentOrderDatabaseTest.java`、`OutboxCrashWorker.java`、`component/OrderOutboxPublisherTest.java`。JDBC凭据只从已有受限测试配置注入环境，异常只输出类型，不展示配置或原始驱动异常。

日志：`completion-outbox-recovery-java-tests-20261114.log`、`completion-outbox-process-crash-java-tests-20261114.log`；最终12项的用例名与surefire结果哈希见`completion-outbox-process-crash-suites-20261114.json`。明确传入skipTests=false与maven.test.skip=false，避免父POM默认跳过。

## 打包和部署

测试全部结束、只读确认活动run/job/未决业务/新上传为0、Java操作无IN_TX且outbox无有效发布租约后，打包portal并保留原JAR备份，仅安装portal、重载agent-api和agent-scheduler。没有重启管理器、SSH隧道、其他商城服务或远端中间件。

打包日志`completion-recovery-portal-package-20261114.log`中的跳过测试仅指该次package步骤；上面的28/12是此前实际执行的测试，不能混淆。

当前portal JAR SHA256：`863aadaab147fc51ea276ad08203b0ea6fd1ea32d618105acf3a4c0bb3a22394`。已核对运行暂存包与构建包一致，JAR内OrderBusinessOutbox.class与target/classes一致。gateway JAR哈希与切换前相同；迁移0010、130设计操作、101实际Python操作不变。

安装和就绪证据：`completion-recovery-preinstall-20261114.json`、`completion-recovery-portal-install-20261114.log`、`completion-recovery-api-reload-20261114.log`、`completion-recovery-scheduler-reload-20261114.log`、`completion-recovery-postinstall-ready-20261114.json`。

## 真实网关的拒绝、人工续签与原结果恢复

1. 专用会员22通过实际API创建标记会话和ADD_CART确认卡，点击确认的等价认证接口执行一次，SKU239数量1加入购物车。调用前不存在该SKU的活动购物车行。
2. 只对该新建且归属/标题/编号匹配的PG action执行受控故障注入，模拟结果未保存并将原核对凭据签名改坏。Java业务账本、购物车和库存不作SQL修改。
3. 当前已部署scheduler调用真实Java核对端点，收到凭据拒绝，实际状态为UNKNOWN、MANUAL_REQUIRED、RECONCILE_GRANT_REJECTED，attempts为1、fence为2。没有把它标成业务失败，也没有自动再次加购。
4. 通过当前员工登录的真实管理API读取未决列表并续签，scheduler使用原操作编号核对。最终SUCCEEDED/TERMINAL，attempts为2、fence为4；Java操作账本、购物车行及库存的完整指纹与注入前相同。
5. 通过原商城购物车API删除本轮新增的那一行，活动购物车恢复原列表；通过会员API删除本轮会话并核对404。业务操作及审计保留。

这证明当前运行代码已处理真实Java凭据拒绝及员工续签，不只在合成单测中通过。故障注入的PG修改限定本轮新建TEST ONLY对象，不修改原订单/退货或原业务账本；不以直接写入一个“成功”记录冒充恢复。

脚本：`mall-agent/scripts/smoke_recovery_rejected_grant.py`。状态证据：`completion-rejected-grant-live-20261114.json`；prepare/inject-final/resume/cleanup同前缀日志各自保留。

首次inject辅助脚本缺少运行管理器注入的基础配置，Settings校验失败，**发生在PG注入之前**。只记录ValidationError类型及所缺字段名，没有输出原始配置/校验异常；改为读取既有受限action密钥配置后完成同一夹具，没有重复加购。首次失败日志`completion-rejected-grant-inject-20261114.log`保留。

## 最终核对与剩余范围

最终livez/readyz均200，九类依赖UP，六类worker新鲜，活动run/job/未决业务/新上传均0。原订单116、退货38、物流和SKU239库存行完整指纹与安装前一致；3份演示知识、6笔未知用量和备份策略版本3保留。原始Word SHA256不变，原演示消耗两件库存未重复回补。

Ruff全量check、五个改变Python文件格式、共享契约及离线框架检查通过。本轮没有前端改动，不重复运行无关前端构建或新增容量测试。

最终状态见`deployment/.run/completion-recovery-final-state-20261114.json`。第9组当前新增的凭据拒绝、依据校验、旧租约、进程退出及通知丢失/事务回滚证据已补；真实全链路中更广的接管/删除/凭据失效交叉窗口、 broker故障组合、完整恢复后重新开放仍需统一验证。原其他组的多轮/四卡、流式体验、混合事实故障、真实费用依据、留存竞态和灾难恢复继续推进，目标保持active。
