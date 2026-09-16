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
- 视频成品可在任务详情中直接加入 ffplayout 媒体库；服务端优先使用 hardlink，失败时才回退为 `copy2`。

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
    ├── ffplayout（独立 SQLite 播控子系统）
    └── outbox-dispatcher

Redis / PostgreSQL（外部） ◄── 队列、任务与产物状态
共享文件系统 `/storage` ◄── 所有视频、音频、字幕和日志
egress-gateway（唯一允许访问公网的抓取出口）
```

除 Web 外的服务均不发布宿主机端口；内部请求使用服务身份 token。完整说明见[架构指南](docs/ARCHITECTURE.md)。

## 环境要求

- Docker Engine 与 Docker Compose Plugin
- PostgreSQL 16+，启用 `pgvector`
- 可用的网络和一个可写的共享存储目录
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

本地默认入口为 `http://localhost:3000`，登录后从左侧“播控中心”进入
ffplayout。默认情况下播控地址自动沿用当前浏览器 hostname，只切换到专用端口
`3003`，因此通过 LAN IP、VPN 地址或不同 DNS 名称访问时不需要重建 Web 镜像。

## 生产离线部署

构建机生成完整离线包：

```bash
git submodule update --init --recursive
ENV_FILE=/path/to/production.env INCLUDE_BASE_IMAGES=1 ./scripts/build_export_prod.sh
```

包内包含应用、egress gateway、Web、社交发布器、ffplayout 和 Redis 镜像。目标机只需保留 Compose、私有 `.env` 和共享存储目录：

```bash
sha256sum -c videoroll-prod-bundle-*.tar.sha256
docker load -i videoroll-prod-bundle-*.tar
docker compose --env-file .env up -d --no-build --remove-orphans
```

不要覆盖已有的 `STORAGE_HOST_ROOT`、`data/models`、`data/secrets` 或 `data/redis`；数据库连接也应保留。完整上线、GPU 和回退步骤见[部署指南](docs/DEPLOYMENT.md)。

## 关键生产变量

| 变量 | 要求 |
|---|---|
| `DATABASE_URL` | 指向外部 PostgreSQL，生产已有连接应保持不变。 |
| `STORAGE_HOST_ROOT` | 宿主机共享文件根目录，默认使用部署目录下的 `./data/storage`。 |
| `STORAGE_ROOT` | 容器内对象目录，默认 `/storage/objects`。 |
| `INTERNAL_API_SECRET` | 随机且非空，用于内部服务身份与管理员 cookie 密钥派生。 |
| `ADMIN_BOOTSTRAP_SECRET` | 随机且非空，仅用于首次管理员初始化。 |
| `PUBLISH_ADDR` | Web 唯一宿主机绑定地址；通常先使用 `127.0.0.1` 并由反向代理公开。 |
| `PLAYOUT_PORT` | ffplayout 浏览器入口端口，默认 `3003`；宿主机映射到 Web/Nginx 的专用监听端口。 |
| `VITE_FFPLAYOUT_PORT` | 前端默认使用的播控端口，默认 `3003`；通常与 `PLAYOUT_PORT` 保持一致。 |
| `VITE_FFPLAYOUT_URL` | 可选的完整播控 URL 覆盖值；留空时自动使用当前浏览器 hostname + `VITE_FFPLAYOUT_PORT`。 |
| `LEGACY_LIVE_ENABLED` | 旧 VideoRoll Live 引擎紧急回退开关；生产默认 `false`。 |
| `SUBTITLE_ASR_ENGINE=openvino` | Intel GPU ASR 使用 OpenVINO。 |
| `SUBTITLE_OPENVINO_DEVICE=GPU` | Intel GPU OpenVINO 设备名。 |
| `INTEL_GPU_RENDER_GID` | 宿主机 `/dev/dri/renderD128` 的组 ID。 |

从[.env.example](.env.example)开始配置；真实密钥、Cookie、数据库密码和 `data/secrets/fernet.key` 永远不能提交到 Git。

默认生产部署不要求固定播控域名。Web 入口使用 `WEB_PORT`，播控入口使用
`PLAYOUT_PORT`；前端会根据当前浏览器 hostname 自动生成播控地址。例如：

```dotenv
WEB_PORT=3001
PLAYOUT_PORT=3003
VITE_FFPLAYOUT_PORT=3003
VITE_FFPLAYOUT_URL=
LEGACY_LIVE_ENABLED=false
```

因此从 `http://192.168.5.23:3001` 进入时播控自动使用
`http://192.168.5.23:3003`；从其他 IP/DNS 名称进入时也自动跟随相同 hostname。
不要向宿主机发布 ffplayout 的 8787；它只在 Compose `internal` 网络中供 Web
nginx 访问。

任务详情的“媒体与资产”页只允许将 `video_final` 成品加入播控。该操作只提交
`task_id` 和 `asset_id`，由 Orchestrator 在服务端把文件链接/复制到
`/storage/playout-media/VideoRoll/<task-id>/`，不会经过浏览器重新下载和上传。
加入成功后该文件归 ffplayout 播控媒体库管理；以后删除 VideoRoll 原成品不会
自动删除播控媒体，避免正在使用的 Playlist 出现失效引用。

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
