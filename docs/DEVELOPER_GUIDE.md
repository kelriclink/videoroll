# 开发者指南

本文档面向需要在本地开发、调试或扩展 VideoRoll 的开发者。

## 仓库结构

```
src/videoroll/apps/   Orchestrator、字幕、接入、投稿与 egress 服务
src/videoroll/db/     SQLAlchemy 模型、Alembic 迁移与 outbox/inbox
src/videoroll/storage/ S3/MinIO 封装
src/videoroll/ai/     翻译、embedding、RAG 与模型客户端
src/videoroll/utils/  加密、内部 token 与共享工具
src/web/              React 18 + Vite + Tailwind 前端
tests/                后端测试（pytest）
docs/                 架构、部署、接口与开发文档
scripts/              开发、检查、镜像构建与导出脚本
Dockerfile            应用与 egress gateway 镜像
docker/               entrypoint、社交发布器镜像与运行脚本
docker-compose.yml    进程隔离的默认 Compose 拓扑
docker-compose.intel.yml  Intel iGPU 覆盖层
```

## 开发环境命令

```bash
# 启动全部服务（含 Intel GPU 透传）
./scripts/dev_up.sh

# 停止服务
./scripts/dev_down.sh

# 健康检查
./scripts/dev_health.sh

# 查看日志
./scripts/dev_logs.sh

# 仅前端开发服务器（Vite, port 3000）
./scripts/dev_web.sh

# 后端测试
python -m pytest tests/

# 前端 lint / 构建
cd src/web && npm run lint && npm run build

# 本地冒烟测试
./scripts/smoke_local.sh [video.mp4]
```

## 构建与部署

```bash
# 构建并导出完整离线生产包（包含 egress gateway）
ENV_FILE=.env INCLUDE_BASE_IMAGES=1 ./scripts/build_export_prod.sh
```

生产离线部署、GPU、迁移与回退见[部署指南](DEPLOYMENT.md)。

## 安全上线与运行

生产部署只公开 `web` 的 `${PUBLISH_ADDR}:${WEB_PORT}`。`orchestrator`、四个内部 API、Redis、MinIO、outbox dispatcher 和 egress gateway 都在 Compose 的 `internal` 网络中，不能添加 `ports:` 映射；需要诊断时使用受控的 `docker compose exec`，不要临时暴露内部端口。

### 必需环境变量

| 变量 | 生产要求 | 用途 |
|---|---|---|
| `DEVELOPMENT_MODE` | `false` | 仅本地开发可设为 `true`；不是关闭认证的开关。 |
| `INTERNAL_API_SECRET` | 随机、非空、非默认值 | 派生内部服务请求 token 和管理员 cookie 密钥。 |
| `ADMIN_BOOTSTRAP_SECRET` | 随机、非空、非默认值 | 一次性初始化管理员账户；成功使用后会被数据库标记为已消费。 |
| `EGRESS_GATEWAY_URL` | `http://egress-gateway:8020` | RAG/网页抓取唯一允许使用的出站网关。 |
| `ORCHESTRATOR_URL` | `http://orchestrator:8000` | 内部 worker 回调地址，不能指向公网 URL。 |
| `PUBLISH_ADDR` | 通常 `127.0.0.1` | 唯一允许的 Web 宿主机绑定地址。 |

从 `.env.example` 开始，使用密码管理器或部署系统注入两个 secret；不要将真实值提交到仓库。`./scripts/dev_up.sh` 仅为首次本地开发生成唯一 secret，不能替代生产密钥管理。

### 上线阶段与回退边界

1. 备份数据库，并确认现有任务、publish jobs 与发布批次可读。
2. 运行 `python -m videoroll.db.migrate upgrade`，再启动 `egress-gateway`、内部 API/worker、`outbox-dispatcher` 和最后的 `web`。
3. 运行 `./scripts/security_smoke.sh`；该检查不联网、不启动容器。
4. 观察 outbox pending 年龄、失败重试、内部鉴权失败、egress 拒绝和 desktop grant 拒绝日志，再允许生产流量。

当前没有允许降级到旧安全模型的 feature flag。`DEVELOPMENT_MODE`、`DEPLOYMENT_ROLE`、服务副本数和 dispatcher 启停只能控制本地开发或部署拓扑，不能恢复 query-token、未认证 noVNC、直接内部端口或任意出站请求。出现问题时可以回退应用版本、停止新流量或扩容 dispatcher，但保留 schema 与安全边界。

### Outbox 观察与修复

所有可恢复的异步副作用首先写入 `outbox_events`，再由 `outbox-dispatcher` 投递。broker 失败会释放事件并以指数退避重试；dispatcher lease 过期可被其他 dispatcher 认领。

- 首先确认 `outbox-dispatcher` 健康且 Redis 可用：`docker compose ps outbox-dispatcher redis`。
- 查看 `outbox_events` 中的 `status`、`attempt_count`、`available_at`、`lease_until` 和 `last_error`，不要直接删除 pending/failed 行。
- 修复 broker 或 worker 后重启/扩容 dispatcher；正常调度会重新认领到期事件。
- 仅当确认 worker 从未达到外部副作用边界时，才能对已投递但未启动的操作执行受控重投；`unknown` 发布状态必须由管理员确认，不能靠重启或 SQL 强行重试。

### Desktop 授权

管理员先通过 `POST /api/desktop/grants` 创建 login 或 publish grant；grant 绑定管理员会话、desktop 类型和资源 UUID，默认 5 分钟有效，WebSocket 重连次数受限。浏览器 URL 中的 grant 是短期授权材料，不是 VNC 密码：不要复制到工单、日志或 Referer。Nginx 会对 noVNC landing page 与 WebSocket 发起授权子请求；没有管理员会话、过期 grant、错误资源或超出重连上限都会被拒绝。

VNC 进程密码只存在容器 tmpfs；它不应出现在 URL、数据库、前端配置或日志。部署 interactive desktop 前，应额外验证 noVNC 的受信任密码握手路径已随当前镜像启用。

### Egress 私网要求

RAG/页面抓取只能调用 egress gateway。网关对每次 DNS 结果、重定向目标和实际连接 peer 都要求全局可路由地址，拒绝 loopback、RFC1918、link-local、metadata 与混合 DNS 结果；不能通过 hosts、代理或 URL 凭证绕过。应用容器不应直接获得任意公网出口。

## 架构要点

### 进程角色

Compose 不再把多个 API 或 worker 组合进同一 PID。`orchestrator`、内部 API、发布器进程、worker、dispatcher 和 `egress-gateway` 都是独立容器进程；相同应用镜像通过不同 `command` 与 `DEPLOYMENT_ROLE` 启动。服务边界和网络图见[架构指南](ARCHITECTURE.md)。

浏览器只与 `web` 和 Orchestrator 合约交互，不能直接调用子服务。内部调用通过 Docker DNS、`X-Videoroll-Internal-Token` 与受限代理路径完成。

### 任务状态机

```
CREATED → INGESTED → DOWNLOADED → AUDIO_EXTRACTED → ASR_DONE → TRANSLATED
→ SUBTITLE_READY → RENDERED → READY_FOR_REVIEW → APPROVED → PUBLISHING → PUBLISHED
```

服务间仅通过 DB 任务状态 + S3 存储 key 通信，不直接传递数据。

### Celery、outbox 与恢复

| 角色 | 队列/职责 | 关键任务 |
|---|---|---|
| `subtitle_service.worker` | `subtitle` | `task_queue_tick`（调度器）、`process_job`（ASR+翻译）、`process_render_job`（ffmpeg 压制）、`auto_youtube_pipeline`、`after_render_publish`、`cleanup_task` |
| `bilibili_publisher.worker` | `publish` | `process_job`（上传 B 站） |
| `social_publisher.worker` | `social_publish` | 账号校验、受控浏览器投稿与状态回写 |
| `outbox-dispatcher` | durable outbox | 认领、投递和重试副作用事件 |

worker 使用 operation key、inbox 和 lease/heartbeat，过期任务可恢复。外部发布状态必须先对账，不能把 `unknown` 直接重试。

### 数据库

PostgreSQL + psycopg 3 与 SQLAlchemy 2 ORM。生产 schema 由 Alembic 管理：`python -m videoroll.db.migrate upgrade`。旧 `auto_migrate` 仅用于有限兼容路径，不能替代升级迁移。

### 配置

每个服务一个 pydantic-settings `Settings` 类，继承 `CommonSettings`。环境变量驱动，支持 `.env` 文件，`@lru_cache` 缓存。

### 前端

React 18 + TypeScript + Vite + Tailwind + react-router-dom v6。生产环境 nginx 托管 SPA 并反代 `/api/`。开发时 Vite 反代 `/api` 到 `localhost:8000`；前端只保留 Orchestrator API base URL，子服务地址不会注入浏览器构建产物。

### 外部服务

- **Redis** — Celery broker/backend
- **MinIO** — S3 兼容对象存储
- **PostgreSQL 16+** — 需外部提供

## 编码约定

- Python ≥3.12，`snake_case` 模块/函数，`PascalCase` 类/Pydantic 模型
- 4 空格缩进，类型注解
- 前端组件 `PascalCase`，helpers 命名如 `videosPage.helpers.ts`
- 测试文件 `*.test.ts`（前端）或 `tests/test_<feature>.py`（后端）
- 提交信息使用 Conventional Commit 前缀：`feat:`、`fix:` 等

## 安全注意事项

详见 [docs/SECURITY_AUDIT.md](SECURITY_AUDIT.md)。

要点：

- 不要提交真实的 cookies、API keys 或 `data/secrets/fernet.key`
- 密钥丢失 = 所有加密设置不可恢复
- RAG 向量搜索需要 pgvector 扩展
- 新环境变量需记录在 `.env.example`
