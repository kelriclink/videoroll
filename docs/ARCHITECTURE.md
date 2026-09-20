# VideoRoll 架构指南

当前基线：2026-09-20。

VideoRoll 是一个以 Orchestrator 为中心、以 PostgreSQL 为最终状态源、以 Redis/Celery 为异步执行层的视频处理系统。核心目标是把高风险外部访问、浏览器自动化、ASR/Embedding 重依赖和业务编排分开，同时保证任务可以恢复、外部副作用尽量幂等。

## 1. 系统边界

```text
┌──────────────────────────── Browser ────────────────────────────┐
│                                                                 │
│  React SPA                                                      │
│     │                                                           │
│     ▼                                                           │
│  web / nginx                                                    │
│     ├── /api/* ──────────────► orchestrator                     │
│     ├── /api/ws/* ───────────► realtime websocket               │
│     ├── /playout/* ──────────► ffplayout                        │
│     ├── /social-login/* ─────► social-publisher-api noVNC      │
│     └── /social-publish/* ───► social-publisher-worker noVNC   │
└─────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
                         internal service mesh
```

生产默认只有 Web 映射宿主机端口。

## 2. 生产服务

| 服务 | 职责 | 关键依赖 |
|---|---|---|
| `web` | React 静态站点、Nginx 同源网关 | orchestrator, social publisher |
| `orchestrator` | 管理员 API、任务编排、设置、资产、发布、运维 | PostgreSQL, Redis, internal APIs |
| `subtitle-service` | 字幕/ASR/RAG/Embedding API | PostgreSQL, Redis, egress |
| `subtitle-worker` | 重型字幕流水线 Celery worker | models, storage, Redis |
| `subtitle-control-worker` | 队列/控制类 Celery worker | Redis |
| `youtube-ingest` | YouTube source、scan、ingest | platform egress |
| `bilibili-publisher` | Bilibili auth/publish API | PostgreSQL, platform egress |
| `publish-worker` | Bilibili 异步投稿 | Redis, storage |
| `social-publisher-api` | 社交账号、网页登录会话、SAU API | Redis, Chromium |
| `social-publisher-worker` | 抖音/小红书/快手浏览器投稿 | Redis, Chromium |
| `social-publisher-scheduler` | 社交发布定时调度 | Redis |
| `outbox-dispatcher` | durable outbox → Celery | PostgreSQL, Redis |
| `egress-gateway` | 受控 HTTP 外网出口 | internet |
| `redis` | Celery、实时事件、速率/并发辅助状态 | volume |
| `ffplayout` | 播控、playlist、player、media | playout storage |

## 3. 网络模型

`docker-compose.yml` 定义：

- `internal`：`internal: true`，服务间主网络；
- `egress`：egress gateway 的外网；
- `subtitle-egress`：字幕服务所需外网；
- `platform-egress`：YouTube/Bilibili/social 平台访问；
- `playout-egress`：ffplayout 输出链路；
- `infrastructure-egress`：outbox 等基础设施路径；
- `web-ingress`：Web 对外入口。

设计原则：

1. 浏览器不直连 subtitle/youtube/publisher；
2. Orchestrator 只代理允许的内部 API；
3. Agent 网页抓取通过受控 egress 路径；
4. 浏览器自动化隔离在 social-publisher 镜像；
5. PostgreSQL 在生产 Compose 外部提供。

## 4. Web 网关

`src/web/nginx.conf` 同时承担：

- SPA fallback；
- `/api/` → Orchestrator；
- `/api/ws/` → WebSocket；
- `/playout/` → ffplayout；
- ffplayout 必须使用的部分 root API namespace；
- `/social-login/` → 登录桌面；
- `/social-publish/` → 发布桌面。

ffplayout upstream 使用 Docker DNS runtime resolver，因此 ffplayout 暂时不可用时 Web 本身仍可启动。

## 5. 管理员身份

管理员认证存储在 `app_settings` 的 `admin.auth` 中。

密码：

- PBKDF2-HMAC-SHA256；
- 200,000 iterations；
- 随机 salt。

管理员登录后使用 `videoroll_admin_device` trusted-device cookie。Cookie 签名 key 同时派生自内部 secret 和当前管理员密码 hash，因此修改密码会使旧 trusted-device cookie 失效。

WebSocket 同样要求有效管理员 device cookie，并执行 Origin 校验。

## 6. 内部服务身份

内部服务请求使用：

```text
X-Videoroll-Internal-Token
```

内部 token 从部署 secret 派生，用于阻止外部请求绕过 Orchestrator 直接模拟内部服务身份。

这不是 mTLS；因此：

- `INTERNAL_API_SECRET` 必须保密；
- 内部 API 不应额外暴露宿主机端口；
- service network 仍应保持最小可达。

## 7. 任务领域模型

主要实体：

- `Task`
- `Asset`
- `Subtitle`
- `SubtitleJob`
- `RenderJob`
- `PublishBatch`
- `PublishJob`
- `YouTubeSource`
- `AppSetting`

可靠性/运维实体包括：

- `OutboxEvent`
- `OperationInbox`
- `RemoteAPIRequest`
- `AIUsageEvent`
- `AlertEvent`
- `DesktopAccessGrant`
- `SecurityAuditEvent`
- `PlayoutAssetLink`

### Task 状态

```text
CREATED
  ↓
INGESTED
  ↓
DOWNLOADED
  ↓
AUDIO_EXTRACTED
  ↓
ASR_DONE
  ↓
TRANSLATED
  ↓
SUBTITLE_READY
  ↓
RENDERED
  ↓
READY_FOR_REVIEW
  ↓
APPROVED
  ↓
PUBLISHING
  ↓
PUBLISHED
```

异常/控制状态：

- `FAILED`；
- `CANCELED`。

停止任务时使用 `CANCELED`，同时保存 `stopped_status`，以便恢复到正确工作阶段。

Task 还包含：

- `priority`；
- `queue_position`；
- retry/error；
- lease/lock；
- publish batch compatibility pointer。

## 8. 资产契约

当前 `AssetKind`：

- `video_raw`
- `metadata_json`
- `audio_wav`
- `segments_json`
- `subtitle_srt`
- `subtitle_ass`
- `video_final`
- `cover_image`
- `log`
- `publish_result`

数据库保存 storage key、sha256、大小和时长；实体文件由共享 `FileStore` 管理。

## 9. 字幕流水线

```text
source video
   │
   ▼
audio extraction
   │
   ▼
ASR
   │
   ▼
segments.json
   │
   ▼
translation stage
   │
   ├── checkpoint resume
   ├── RAG context
   ├── Agent research
   ├── Translation Memory recall
   ├── Translation Plan
   ├── batch translation
   ├── validation
   └── targeted repair
   │
   ▼
SRT / ASS
   │
   ├── subtitle only
   ├── soft subtitle mux
   └── burn-in render
```

### ASR

当前支持：

- faster-whisper；
- OpenVINO GenAI；
- External Whisper-compatible；
- Groq Whisper；
- Cloudflare Workers AI。

Intel/OpenVINO 运行状态可通过：

```text
GET /subtitle/hardware/intel
```

读取 DRM 信息和 `ov.Core().available_devices`。

## 10. RAG Agent

RAG 不是把搜索结果整段附加给翻译模型。

当前流程：

1. 从字幕上下文发现候选术语；
2. Gate 判断是否值得研究；
3. 查询本地 Dictionary / Knowledge Base；
4. 必要时调用 Wikipedia / SearXNG / fetch URL；
5. 子 Agent 通过受控 Tool Registry 执行；
6. Verifier 结构化校验研究结果；
7. 构建可应用到具体 block 的 Translation Plan；
8. 将 Plan、TM 示例和上下文交给翻译阶段。

Agent Runtime 管理：

- timeout；
- LLM call budget；
- tool call budget；
- external request budget；
- input/output/total token；
- cost；
- cancellation；
- provider rate limit；
- lease；
- checkpoint；
- append-only trace。

Tool 不能绕过 `AgentRuntime` 直接执行。

## 11. Agent Skills

Skill 是**提示/工具策略包**，不是任意代码插件。

来源：

- built-in：`src/videoroll/apps/subtitle_service/skills/`；
- user：`data/agent_skills/`；
- override：`VIDEOROLL_AGENT_SKILLS_DIR`。

详细格式见 [AGENT_SKILLS.md](AGENT_SKILLS.md)。

## 12. Translation Plan / Memory / Validation

### Translation Plan

研究 term cards 与用户 glossary 会转成结构化约束：

- `hard`；
- `preferred`；
- `contextual`。

每条约束可以指定适用 block、aliases 和 confidence。

### Translation Memory

`translation_memory_entries` 保存 source/target pair。

当前信任边界：

- 同一 Task 内允许召回机器翻译 memory；
- 跨 Task 只召回 `approved` memory。

### Validator

当前至少检查：

- 空译文；
- 数字变化/丢失；
- hard term 缺失；
- preferred term 未使用。

阻断问题只修复失败 block，不重翻整个 batch。

## 13. Embedding 与 pgvector

知识库允许不同历史模型/维度共存。

查询明确过滤：

- target language；
- embedding model；
- `vector_dims(embedding)`；
- domain/status。

因此数据库列不必等同于“当前设置维度”的固定 `vector(N)`。

运行状态：

```text
GET /subtitle/embedding/runtime
```

会报告：

- provider/model/device；
- configured dimensions；
- pgvector version；
- column type；
- vector buckets；
- HNSW index/usable status；
- actual search mode。

没有可用 ANN 时，查询回退为 exact cosine scan。

## 14. Durable Outbox / Inbox

异步副作用采用 transactional outbox：

```text
business transaction
  ├── domain state
  └── outbox event
          │
          ▼
   outbox-dispatcher
          │
          ▼
       Celery
          │
          ▼
   operation inbox
```

`operation_key` 是最终幂等边界之一。

Dispatcher 支持：

- lease claim；
- PostgreSQL `SKIP LOCKED`；
- failed retry；
- stale lease recovery。

Worker inbox 防止 broker 重复投递造成重复副作用。

## 15. 实时事件

数据库提交后发布 best-effort Redis Pub/Sub 通知。

Redis event 丢失不会破坏状态正确性。客户端：

1. REST 读取 snapshot；
2. WebSocket 接收增量；
3. 断线或 queue overflow 时重新 REST resync。

详见 [realtime-events.md](realtime-events.md)。

## 16. 投稿架构

### Bilibili

独立服务：

- `bilibili-publisher`；
- `publish-worker`。

Bilibili Cookie 加密存储；默认投稿模板带版本冲突处理。

### Social

独立：

- API；
- worker；
- scheduler；
- SAU submodule；
- Chromium/Patchright；
- noVNC 临时桌面。

详见 [social-publisher.md](social-publisher.md)。

## 17. ffplayout

只有 `video_final` 可以加入播控。

服务端将成品映射到：

```text
/storage/playout-media/VideoRoll/<task-id>/
```

传输优先：

1. hardlink；
2. copy fallback。

删除 VideoRoll Asset mapping 不会自动删除 ffplayout media 文件，以避免已存在 playlist 引用失效。

## 18. 数据库迁移

Orchestrator 启动时调用 `initialize_database()`，通过 Alembic upgrade 到 head；migration 失败时服务不会继续启动。

当前 migration head：

```text
0006_agent_runtime
```

生产发布仍建议在滚动切换前显式运行 migration，把 schema 变化和容器重建拆成两个可观测步骤。

## 19. 设计原则

- PostgreSQL 是最终状态源；
- Redis 不承担不可恢复的业务事实；
- 外部副作用必须尽量幂等；
- 浏览器只访问 Web；
- 重依赖和浏览器自动化隔离；
- 网络出口按角色拆分；
- 设置值与运行事实分开显示；
- 失败优先可恢复，而不是自动重复外部投稿。
