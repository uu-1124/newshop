# Newshop 独立演示

买家端、后台和业务接口都从当前 Git 提交的源码构建。中间件和构建镜像使用具体版本标签，中文搜索插件校验 SHA-256。镜像在本机生成，不需要访问原服务器，也不需要私有镜像仓库。本项目使用独立 Docker 卷；已有部署的订单、账号、密钥及知识库数据不会导入。

## 克隆和启动

仓库所有者先给演示者私有仓库读取权限。演示者需要 Git 和 Docker Compose v2，Docker 切到 Linux 容器；建议使用 x86-64 环境，给 Docker 至少 8 GB 内存和 20 GB 可用磁盘。首次构建需要联网获取公开镜像和项目依赖。ARM 环境暂无启动验收记录。

```sh
git clone --branch codex/portable-demo https://github.com/uu-1124/newshop.git
cd newshop/deployment/demo
```

生成本次安装自己的数据库密码和服务间签名密钥。`demo.env` 已被 Git 忽略；有数据时保留原文件。

Linux/macOS：

```sh
if [ ! -f demo.env ]; then
  (umask 077; docker run --rm -i python:3.13.5-slim-bookworm python - < generate-env.py > demo.env)
fi
```

Windows PowerShell：

```powershell
if (-not (Test-Path demo.env)) {
  $demoConfig = Get-Content -Raw generate-env.py | docker run --rm -i python:3.13.5-slim-bookworm python -
  if ($LASTEXITCODE -ne 0) { throw '生成演示配置失败' }
  $demoConfig | Set-Content -Encoding ascii demo.env
}
```

两边用同一条启动命令：

```sh
docker compose --env-file demo.env up --build -d --wait --wait-timeout 480
```

| 入口 | 地址 | 演示账号 |
| --- | --- | --- |
| 买家端 | http://127.0.0.1:18180/ | `demo-buyer` |
| 管理后台 | http://127.0.0.1:18181/ | `demo-admin` |

两个公开的本机演示账号使用密码 `NewshopDemo!2026`。端口冲突时可以改 `demo.env` 内的 `DEMO_SHOP_PORT` 和 `DEMO_ADMIN_PORT`。只在本机打开演示。不要把此配置和演示账号暴露到公网。

买家端可从商品详情进入客服，也可演示购物车、模拟支付和订单；后台可查看商品、发货及处理售后。在客服会话中申请人工接待，后台进入「智能客服 → 客服工作台」，切换在线后领取。初始化包含商品、SKU、图片、收发货地址和售后规则，订单从空库开始。历史营销活动可能已过期。

## 接入自己的模型

默认 `DEMO_AI_PROVIDER=fixture`：可以试商品咨询、流式测试回复和人工接待；测试回复不会发给外部模型，也不会替用户执行 Agent 交易。普通商城订单仍可操作。启用真实对话前，在 `demo.env` 改成以下设置，然后重跑启动命令：

```dotenv
DEMO_AI_PROVIDER=openai_responses
DEMO_AI_URL=https://api.openai.com/v1
DEMO_AI_MODEL=填入该端点支持的模型名
DEMO_AI_KEY=填入个人密钥
```

对话端点需支持 Responses 接口和项目所需的工具调用，使用 HTTPS。要启用视觉，另填 `DEMO_VISION_ENABLED=true` 和 `DEMO_VISION_MODEL`；语音能力使用 `DEMO_ALI_KEY`。知识索引还需要 Milvus：提供 `DEMO_ALI_KEY`、设置 `DEMO_KNOWLEDGE_ENABLED=true`，按以下顺序启动。知识文档由演示者在后台导入、构建和发布。

```sh
docker compose --env-file demo.env --profile knowledge up --build -d vector-init
docker compose --env-file demo.env --profile knowledge wait vector-init
docker compose --env-file demo.env --profile knowledge up --build -d --wait --wait-timeout 480
```

建议在首次启动真实模式前设置好模型。真实模式已初始化后，模型版本由数据库管理；修改环境变量里的模型名不会替换后台已激活的模型版本。项目保留模拟支付、退款和物流；生产备份服务及外部监控不在本演示范围内。

## 验收和停止

默认测试模式下，可以用 Python 3 运行 `python smoke.py`（Linux/macOS 也可用 `python3 smoke.py`）。它会检查双端登录、演示商品和中文搜索、后台上传、流式客服回复、人工接待队列。端口改过时，按顺序把买家端和后台的完整 URL 作为两个参数传给脚本。脚本会在演示库创建一条会话和待接待工单。

```sh
docker compose --env-file demo.env ps -a
docker compose --env-file demo.env logs --tail=100 search-init pg-init object-init
docker compose --env-file demo.env --profile knowledge down
```

停止时会保留演示数据。只有明确要重置这套演示库时，才对最后一条命令加 `--volumes`。若 Elasticsearch 日志要求调整 `vm.max_map_count`，在 Linux 宿主机或 Docker 所在的 Linux 虚拟机按日志提示设置后重试。
