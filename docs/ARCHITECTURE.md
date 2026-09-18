# 架构指南

本文档描述当前生产拓扑，而不是早期单进程原型。Compose 将 HTTP API、worker、调度器和出站访问分成独立进程；core 角色使用精简的 `videoroll:prod`，Subtitle API/worker 使用包含 RAG、embedding 与本地 ASR 依赖的 `videoroll-subtitle:prod`。

## 服务与职责

| 服务 | 职责 | 网络暴露 |
|---|---|---|
| `web` | SPA、Nginx 反向代理与 desktop 授权入口 | 唯一的宿主机端口 |
| `orchestrator` | 管理员认证、任务状态机、资产、设置、浏览器代理 | internal + platform-egress |
| `subtitle-service` | ASR、翻译、RAG、字幕和渲染 HTTP API | internal + subtitle-egress |
| `subtitle-worker` | 执行 subtitle 队列与租约恢复 | internal + subtitle-egress |
| `youtube-ingest` | 受授权来源接入与扫描 | internal + platform-egress |
| `bilibili-publisher` / `publish-worker` | Bilibili 投稿 API 与 publish 队列 | internal + platform-egress |
| `social-publisher-api` / `worker` / `scheduler` | SAU 账号、浏览器投稿和周期调度 | internal + platform-egress |
| `outbox-dispatcher` | 投递 durable outbox，独立于业务 worker | internal |
| `egress-gateway` | RAG 与网页抓取的唯一公网出口 | internal + egress |
| `redis` | 队列与调度状态 | internal |
| `ffplayout` | 独立 SQLite 播控、媒体与 HLS/RTMP/SRT 输出 | internal |

所有需要处理媒体的服务共享同一个只写入 `/storage` 的宿主机挂载；对象 key 是相对于 `/storage/objects` 的路径。
Orchestrator 将 `video_final` 资产以服务端 hardlink（失败时 `copy2`）加入
`/storage/playout-media/VideoRoll/<task-id>/`，映射记录位于 PostgreSQL 的
`playout_asset_links`；ffplayout SQLite 不保存 VideoRoll 业务字段。

## 网络与访问边界

```text
host ── published port ──► web
                                │
                                ▼
                           orchestrator
                                │
                  ┌─────────────┴─────────────┐
                  ▼                           ▼
         internal: true                 platform-egress
       APIs / Redis / DB                   │
            │                              └──► YouTube / Bilibili / social platforms
            ├──► subtitle roles ──► subtitle-egress ──► AI / ASR providers
            ├──► ffplayout ───────► playout-egress ───► RTMP / public stream endpoints
            ├──► outbox ──────────► infrastructure-egress ─► external PostgreSQL / host services
            │
            └──► egress-gateway ──► validated public fetches
```

- `internal` 是 `internal: true` 的东西向 Docker 网络，本身没有默认公网路由；它只承担容器间 API、Redis 等内部通信。
- Subtitle 角色按需加入 `subtitle-egress` 访问 OpenAI/Groq/Cloudflare/Hugging Face 等 provider；平台采集/发布角色加入独立的 `platform-egress`。
- ffplayout 同时加入 `playout-egress`，用于 RTMP/SRT/UDP 等外部推流；outbox-dispatcher 同时加入 `infrastructure-egress`，用于连接宿主机或外部 PostgreSQL。
- RAG 的任意公共网页/Wikipedia/SearXNG 抓取继续通过 `egress-gateway`，由网关执行 URL、DNS、redirect 与连接 peer 校验。
- 只能为 `web` 配置 `ports:`。禁止通过临时端口映射公开 Redis、内部 API、noVNC 或 VNC。
- 浏览器只能请求 Orchestrator 的 `/api` 路由。Orchestrator 使用服务 DNS 与内部 token 转发受允许的请求。

## 身份与交互式桌面

- 所有非 health 的内部 API 请求必须携带 `X-Videoroll-Internal-Token`；token 从 `INTERNAL_API_SECRET` 派生。
- 管理员会话和 bootstrap 均使用独立安全密钥；`DEVELOPMENT_MODE` 不是关闭认证的开关。
- noVNC 不是公开管理端口。管理员先创建绑定会话、资源和类型的短期 desktop grant；Nginx 在 landing page 和 WebSocket 两处校验该 grant。
- VNC 密码只存在于容器 tmpfs，不得写入 URL、数据库、前端配置或日志。

## 任务可靠性

```text
领域事务 + outbox_events
           │
           ▼
outbox-dispatcher ──► Redis / Celery
                         │
                         ▼
                    worker inbox + lease
                         │
                         ▼
                    外部副作用 / 发布状态
```

- 创建可恢复任务时，领域数据与 `outbox_events` 在同一数据库事务提交。
- dispatcher 认领事件后投递；broker 失败会释放事件并按退避策略重试。
- worker 使用 inbox、操作键和 lease/heartbeat 防止重复执行；过期 lease 可安全恢复。
- 发布任务区分 `submitted`、`unknown`、`failed` 和 `published`。`submitted` 与 `unknown` 不自动重投，必须先到平台侧对账。

## 数据与迁移

- PostgreSQL 是任务、设置、审计、outbox/inbox 和发布状态的事实来源；启用 `pgvector`。
- 共享文件系统保存视频、字幕、封面、日志等产物；数据库仅保存元数据与相对存储键。
- `playout_asset_links` 维护 VideoRoll 成品与 ffplayout 文件的映射；加入播控只接受 `task_id` 和 `asset_id`，不接受客户端路径。
- schema 使用 Alembic。上线前运行 `python -m videoroll.db.migrate upgrade`；不要依赖旧的自动加列逻辑完成安全 schema 迁移。
- `data/secrets/fernet.key` 用于加密数据库内的敏感设置。丢失该文件会使已有加密数据不可读。

## 安全验证

代码库提供离线安全检查：

```bash
./scripts/security_smoke.sh
```

该检查验证内部认证、端口隔离、Remote API、desktop grant、outbox 与 egress 拒绝规则。运行态验证、部署顺序和 GPU 配置见[部署指南](DEPLOYMENT.md)。
