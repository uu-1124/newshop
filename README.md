# mall-swarm 本机开发环境

**只想克隆后独立演示？** 走 [Docker Compose 演示部署](deployment/demo/README.md)。它自建数据库、索引和对象存储，提供演示账号与商品；真实 AI 使用演示者自己的密钥。

当前采用 **本机运行 Java 后端、两个 Vue 前端及 Python 智能体，服务器运行中间件容器** 的方式。上游版本固定在 [source-lock.json](deployment/source-lock.json)，工作区包含后续业务修复及智能体增量代码。

截至2026-11-16，当前设计登记20模块、135项操作与12个工作流节点，本机运行时公开契约含107项Python操作，PG迁移0011。本轮完成商品详情客服入口及商品上下文展示、售后规则与知识版本校验、公开价格配置及查询批处理，并按原负载复测；结果、性能限制与回滚步骤见 [48-五项保守修复与版本验收](docs/48-五项保守修复与版本验收.md)，交付指纹见 `deployment/conservative-fixes-manifest.json`。支付、退款和物流保持演示；阶段验收通过不代表整个需求蓝图已经完成。

此前已运行文字/图片咨询、知识检索引用、人工接待、加购/购物车下单及六组扩展，见 [14](docs/14-知识管理与引用闭环验收.md)、[15](docs/15-人工接待闭环实施与验收.md)、[16](docs/16-加购确认与结果核对闭环验收.md)、[17](docs/17-复用Java计价与订单创建闭环验收.md)、[19](docs/19-扩展功能实施跟踪.md)。启动见 [mall-agent/README](mall-agent/README.md)，运行基础与结构清单见 [13](docs/13-运行基础实施与验收.md)、[12](docs/12-整体工程框架与模块落地清单.md)。

## 访问

| 功能 | 地址 | 演示账号 |
| --- | --- | --- |
| 买家端，支持前端热更新 | http://127.0.0.1:5173/ | `member` / `MallDemo2026!` |
| 管理后台，支持前端热更新 | http://127.0.0.1:5100/ | `admin` / `MallDemo2026!` |
| 员工人工接待 | http://127.0.0.1:5100/#/agent/service | 使用后台登录态，专用接待角色 |
| 员工知识管理 | http://127.0.0.1:5100/#/agent/knowledge | 使用后台登录态，新增专用知识角色 |
| 员工订单售后与演示物流 | http://127.0.0.1:5100/#/agent/operations | 使用后台登录态，专用售后/物流权限 |
| 智能体架构演示 | http://127.0.0.1:5173/#/pages/agent/chat | 使用买家登录态 |
| 后端网关 | http://127.0.0.1:8201/ | 业务接口需要登录令牌 |
| Spring Boot Admin | http://127.0.0.1:8101/ | 官方内存账号 `macro` / `123456`，仅本机监听 |
| Nacos | http://127.0.0.1:18848/nacos/ | 开发环境内部控制台 |
| MinIO 控制台 | http://127.0.0.1:19001/ | 见本机 `deployment/secrets.json` |

模拟支付已启用，买家端的支付宝/微信选项均走原项目模拟支付接口，不会真实扣款。

## 运行与修改

本机现在已经运行。以后在 PowerShell 中执行：

```powershell
& D:\newshop\deployment\start-local.ps1
& D:\newshop\deployment\stop-local.ps1
```

第一个命令启动 SSH 隧道、演示图片服务、6 个 Java 服务和两个前端开发服务器；启用智能体配置时还启动 API、generation、scheduler、jobs、outbox、backups、observability 七类进程。第二个命令停止受管理的本机进程树。服务器中间件不受停止本机的操作影响。SSH 断线会自动尝试重连，连接恢复前业务接口可能报错。

源码位置：`mall-swarm/` 是 Java 后端，`mall-admin-web/` 是管理前端，`mall-app-web/` 是买家前端。前端保存源码后由 Vite 热更新。

后端修改后可运行以下脚本，编译成功后会重启本机开发环境并加载新包；编译失败时不会停止现有进程：

```powershell
& D:\newshop\deployment\rebuild-backend.ps1
```

智能体启用后 portal/gateway/admin 使用 `.run/agent-jars` 中的暂存包，完整重建脚本会同步刷新。只构建这三个模块时可使用 [智能体构建和切包流程](mall-agent/README.md)。

也可用 IDE 启动某个后端模块；先停止该模块当前的 Java 进程，再给 IDE 配置下面的启动参数，其余服务和 SSH 隧道继续运行。以 admin 为例：

```text
--spring.profiles.active=deploy
--spring.config.additional-location=file:D:/newshop/deployment/config/admin.yaml
```

JDK 17：`D:\develop\jdk-17`；Maven：`D:\develop\maven`；Node 22：`D:\develop\nodejs`。不同电脑可修改脚本参数或设置 `MALL_JAVA_HOME`。运行日志在 `deployment/.run/`，启动时各服务需要一段初始化时间，管理器启动成功不等于全部服务已经就绪。

## 服务器与连接

服务器 `113.249.103.102` 的 `/opt/mall-swarm/` 保留以下 7 个中间件容器及其数据：MySQL 8、Redis 7、MongoDB 7、RabbitMQ 3、Nacos 2、Elasticsearch 8.18（IK 8.18）、MinIO。另在 `/opt/mall-agent/` 部署独立 PostgreSQL 16，在 `/opt/mall-agent-vector/` 部署 Milvus、etcd 和专用 MinIO 三容器栈。聊天图片使用商城 MinIO 中的独立私有桶，详情见 11。

中间件只绑定服务器 `127.0.0.1`，通过已有 SSH 22 端口转发到本机，无需开放公网 80 或 9000 端口。

| 本机端口 | 服务器中间件端口 | 用途 |
| --- | --- | --- |
| 13306 | 3306 | MySQL，数据库 `mall`，账号 `mall` |
| 16379 | 6379 | Redis |
| 27018 | 27017 | MongoDB |
| 15672 | 5672 | RabbitMQ AMQP，商城虚拟主机 `/mall`，智能体隔离虚拟主机 `/mall-agent` |
| 15673 | 15672 | RabbitMQ 管理页面 |
| 18848 / 19848 | 8848 / 9848 | Nacos HTTP / 客户端 gRPC |
| 19200 | 9200 | Elasticsearch |
| 19000 / 19001 | 9000 / 9001 | MinIO API / 控制台 |
| 15432 | 15432 | 智能体 PostgreSQL；数据库 mall_agent |
| 19530 / 19091 | 19530 / 19091 | 智能体 Milvus RPC / 健康检查 |
| 18080 | 本机资源目录 | 演示图片服务 |

连接凭据保存在本机 `deployment/.dev-ssh.json`、`deployment/secrets.json`，生成配置在 `deployment/config/`、`deployment/compose.yaml`。这些文件和运行日志、jar、node_modules 均已排除 Git；请保留这些本机配置。服务器上的 Compose 文件也包含连接密码，权限为 600。

后台服务：`admin:8080`、`portal:8085`、`search:8081`、`auth:8401`、`gateway:8201`、`monitor:8101`，均只监听本机。Java 服务使用统一 JWT 配置，并以 `127.0.0.1` 注册到远程 Nacos，供本机网关和 Feign 调用。

## 演示数据和本次验证

首次导入的官方SQL共38条商品记录、96条SKU、40条分类，当时未删除且上架、可浏览的商品为20个。已缓存177张图片；原始数据中4张失效图片使用“演示图片暂缺”占位。演示活动时间保留官方数据，部分历史活动已经过期；当前业务数量以实时查询为准。

已通过实际请求验证：6 个 Java 服务健康检查、两个前端、双端登录、统一认证的两条 Feign 登录链路、商品/分类/订单查询、中文搜索、MongoDB 收藏列表、图片上传/读取/删除，以及下单→模拟支付→后台发货→确认收货、未付款订单取消和锁定库存释放。

验收生成的订单 77 已完成、订单 78 已取消，保留用于查看。验收请求及库存前后结果保存在本机 `deployment/evidence/acceptance.json`。这是部署验收，未对全项目做漏洞审计，也没有修改上游业务缺陷。

`deployment/acceptance.cjs` 默认进行查询检查；添加 `--write-demo` 会创建新的演示订单并消耗一件演示库存，日常开发不需要反复运行它。`deployment/init/01-mall.sql` 是首次初始化用的数据，包含重建表语句；已有业务数据后不要重新导入。

## 旧项目备份

旧本机项目移至 `D:\newshop-backup-20261108`。旧服务器项目和数据库备份在 `/opt/project-backups/newbee-before-mall-swarm-20261108/`。旧项目容器已移除。Docker 引擎运行目录保留用于新中间件，不属于旧业务程序。

2026-11-12订单阶段的版本字符串为 `order-0.9`、PG迁移0007，见[当时验收](docs/20-订单售后与直购实施跟踪.md)；之后的迁移与功能以本页开头及文档48为准。CART/DIRECT报价与确认下单、未付款取消、本人订单查询、退货处理和演示物流已接入。真实支付/退款按用户要求不接入。

2026-11-12 结算金额门槛与优惠尾差修复 `ORDER_CHECKOUT_CENTS_V2` 已切换至运行包。此前 66 项 Java 回归及最终源码补测、Python 离线专项、两端类型检查和构建均通过；现已完成真实网关旧卡失效、重复确认、原编号恢复、智能体及普通下单与关闭恢复，并核对浏览器确认卡至原订单详情。测试订单 110、111 已取消，库存与积分恢复；未支付、未调用模型、未重启服务器容器。该结算阶段部署记录见 `deployment/pricing-manifest.json`，当前以文档20为准；该阶段完整证据见 [本轮结果与下一步](docs/18-结算金额修复与质量检查收口.md)。
