# 架构指南

本文档描述当前生产拓扑，而不是早期单进程原型。Compose 将 HTTP API、worker、调度器和出站访问分成独立进程；同一个 `videoroll:prod` 镜像按不同命令承担多个角色。

## 服务与职责

| 服务 | 职责 | 网络暴露 |
|---|---|---|
| `web` | SPA、Nginx 反向代理与 desktop 授权入口 | 唯一的宿主机端口 |
| `orchestrator` | 管理员认证、任务状态机、资产、设置、浏览器代理 | internal |
| `subtitle-service` | ASR、翻译、RAG、字幕和渲染 HTTP API | internal |
| `subtitle-worker` | 执行 subtitle 队列与租约恢复 | internal |
| `youtube-ingest` | 受授权来源接入与扫描 | internal |
| `bilibili-publisher` / `publish-worker` | Bilibili 投稿 API 与 publish 队列 | internal |
| `social-publisher-api` / `worker` / `scheduler` | SAU 账号、浏览器投稿和周期调度 | internal |
| `outbox-dispatcher` | 投递 durable outbox，独立于业务 worker | internal |
| `egress-gateway` | RAG 与网页抓取的唯一公网出口 | internal + egress |
| `redis` / `minio` / `minio-init` | 队列、对象存储与存储桶初始化 | internal |

`minio-init` 是一次性服务，成功完成后退出。其余服务使用健康检查和 `restart: unless-stopped` 运行。

## 网络与访问边界

```text
host ── published port ──► web
                                │
                                ▼
                           orchestrator
                     ┌──────────┼──────────┐
                     ▼          ▼          ▼
                internal APIs  workers  Redis / MinIO
                     │
                     └──► egress-gateway ──► public Internet
```

- `internal` Docker 网络标记为 `internal: true`；除 `egress-gateway` 外，应用进程不应加入可出网网络。
- 只能为 `web` 配置 `ports:`。禁止通过临时端口映射公开 Redis、MinIO、内部 API、noVNC 或 VNC。
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
- MinIO/S3 保存视频、字幕、封面、日志等产物；数据库仅保存元数据与对象键。
- schema 使用 Alembic。上线前运行 `python -m videoroll.db.migrate upgrade`；不要依赖旧的自动加列逻辑完成安全 schema 迁移。
- `data/secrets/fernet.key` 用于加密数据库内的敏感设置。丢失该文件会使已有加密数据不可读。

## 安全验证

代码库提供离线安全检查：

```bash
./scripts/security_smoke.sh
```

该检查验证内部认证、端口隔离、Remote API、desktop grant、outbox 与 egress 拒绝规则。运行态验证、部署顺序和 GPU 配置见[部署指南](DEPLOYMENT.md)。
