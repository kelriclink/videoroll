# VideoRoll

VideoRoll 是用于处理已获授权视频的流水线：YouTube 接入、语音识别、字幕翻译、压制封装，以及 Bilibili、抖音、小红书和快手投稿。它采用进程隔离的编排架构：浏览器只访问 Web 与 Orchestrator，任务副作用由独立 worker 和 durable outbox 处理。

只可处理你拥有版权、已获授权或明确允许再分发的内容；不得用来绕过平台规则或批量搬运未授权内容。

## 核心能力

- RAG Agent 翻译：术语 gate、可信来源检索、verifier 校验和 pgvector 知识库。
- ASR、翻译、字幕压制与自动流水线；Intel iGPU 可通过 OpenVINO 加速 ASR。
- 受白名单和许可信息约束的 YouTube 接入。
- Bilibili 与社交平台投稿，社交浏览器运行时与主应用隔离。
- 管理员认证、短期 desktop grant、内部服务身份认证与审计记录。
- outbox/inbox、租约和发布状态恢复，避免 broker 故障或 worker 重启导致重复副作用。

## 运行架构

```text
浏览器
  │  仅访问 Web（唯一宿主机端口）
  ▼
web / nginx ──► orchestrator
                    ├── subtitle-service + subtitle-worker
                    ├── youtube-ingest
                    ├── bilibili-publisher + publish-worker
                    ├── social-publisher-api + worker + scheduler
                    └── outbox-dispatcher

Redis / MinIO / PostgreSQL（外部） ◄── 所有任务与产物状态
egress-gateway（唯一允许访问公网的抓取出口）
```

除 Web 外的服务均不发布宿主机端口；内部请求使用服务身份 token。完整说明见[架构指南](docs/ARCHITECTURE.md)。

## 环境要求

- Docker Engine 与 Docker Compose Plugin
- PostgreSQL 16+，启用 `pgvector`
- 可用的网络和对象存储凭据
- 可选：Intel iGPU 与 `/dev/dri/renderD128`（OpenVINO ASR）

## 本地开发

```bash
git clone git@github.com:kelriclink/videoroll.git
cd videoroll
git submodule update --init --recursive

# 首次运行会创建带随机开发密钥的 .env
./scripts/dev_up.sh

# 检查服务与日志
./scripts/dev_health.sh
./scripts/dev_logs.sh
```

首次打开 Web 后创建管理员账户，再在设置页配置 LLM、RAG、YouTube、投稿平台和 ASR 参数。

## 生产离线部署

构建机生成完整离线包：

```bash
git submodule update --init --recursive
ENV_FILE=/path/to/production.env INCLUDE_BASE_IMAGES=1 ./scripts/build_export_prod.sh
```

包内包含应用、egress gateway、Web、社交发布器、Redis、MinIO 和 MinIO Client 镜像。目标机只需保留 Compose、私有 `.env` 和现有 `data/` 目录：

```bash
sha256sum -c videoroll-prod-bundle-*.tar.sha256
docker load -i videoroll-prod-bundle-*.tar
docker compose --env-file .env up -d --no-build
```

不要覆盖已有的 `data/minio`、`data/models`、`data/work`、`data/secrets` 或 `data/social-publisher`；数据库连接也应保留。完整上线、迁移、GPU 和回退步骤见[部署指南](docs/DEPLOYMENT.md)。

## 关键生产变量

| 变量 | 要求 |
|---|---|
| `DATABASE_URL` | 指向外部 PostgreSQL，生产已有连接应保持不变。 |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | 随机、私有的 MinIO/S3 凭据。 |
| `INTERNAL_API_SECRET` | 随机且非空，用于内部服务身份与管理员 cookie 密钥派生。 |
| `ADMIN_BOOTSTRAP_SECRET` | 随机且非空，仅用于首次管理员初始化。 |
| `PUBLISH_ADDR` | Web 唯一宿主机绑定地址；通常先使用 `127.0.0.1` 并由反向代理公开。 |
| `SUBTITLE_ASR_ENGINE=openvino` | Intel GPU ASR 使用 OpenVINO。 |
| `SUBTITLE_OPENVINO_DEVICE=GPU` | Intel GPU OpenVINO 设备名。 |
| `INTEL_GPU_RENDER_GID` | 宿主机 `/dev/dri/renderD128` 的组 ID。 |

从[.env.example](.env.example)开始配置；真实密钥、Cookie、数据库密码和 `data/secrets/fernet.key` 永远不能提交到 Git。

## 文档

| 文档 | 内容 |
|---|---|
| [架构指南](docs/ARCHITECTURE.md) | 进程边界、网络、内部认证、outbox/inbox 与恢复语义。 |
| [部署指南](docs/DEPLOYMENT.md) | 在线/离线部署、迁移、Intel GPU、验证与回退。 |
| [开发者指南](docs/DEVELOPER_GUIDE.md) | 代码组织、调试与测试命令。 |
| [远程 API](docs/REMOTE_API.md) | Bearer `POST`、JSON 与幂等键合约。 |
| [社交平台投稿](docs/social-publisher.md) | SAU、账号导入和受控浏览器登录。 |
| [安全审计](docs/SECURITY_AUDIT.md) | 安全上线边界与历史审计记录。 |
| [项目规格](docs/PROJECT_SPEC.md) | 产品能力、产物契约与任务模型。 |
| [Agent Skills](docs/AGENT_SKILLS.md) | RAG Agent skill 能力包格式。 |
