# VideoRoll 开发者指南

当前基线：2026-09-20。

## 1. 仓库结构

```text
.
├── src/videoroll/
│   ├── ai/
│   ├── apps/
│   │   ├── orchestrator_api/
│   │   ├── subtitle_service/
│   │   ├── youtube_ingest/
│   │   ├── bilibili_publisher/
│   │   ├── social_publisher/
│   │   ├── egress_gateway/
│   │   └── outbox/
│   ├── db/
│   ├── storage/
│   └── realtime.py
├── src/web/
├── migrations/
├── tests/
├── scripts/
├── docker/
├── services/ffplayout/
├── social-auto-upload/
├── docker-compose.yml
├── docker-compose.intel.yml
└── docs/
```

## 2. Python 环境

要求：

```text
Python >= 3.12
```

主依赖在 `pyproject.toml` 固定版本。

开发安装：

```bash
python -m venv venv
venv/bin/pip install -c requirements.lock -e '.[dev]'
```

字幕依赖：

```bash
venv/bin/pip install -c requirements.lock -e '.[subtitle]'
```

ASR：

```bash
venv/bin/pip install -c requirements.lock -e '.[subtitle,asr]'
```

## 3. Frontend

要求：

```text
Node >=22 <23
```

```bash
cd src/web
npm ci
npm run dev
```

验证：

```bash
npm run lint
npm test
npm run build
```

## 4. 一键开发环境

```bash
git submodule update --init --recursive
./scripts/dev_up.sh
```

辅助：

```bash
./scripts/dev_health.sh
./scripts/dev_logs.sh
./scripts/dev_down.sh
./scripts/dev_web.sh
./scripts/smoke_local.sh
./scripts/security_smoke.sh
```

## 5. Compose 文件

主文件：

```text
docker-compose.yml
```

Intel GPU override：

```text
docker-compose.intel.yml
```

仓库另有 `compose.yml` 和 `fromprod/docker-compose.yml` 用于兼容/生产工作流。

脚本和 CI 应显式传 `-f`，不要依赖 Compose 自动选文件。

## 6. Orchestrator

入口：

```text
src/videoroll/apps/orchestrator_api/app.py
```

Router：

```text
routers/
├── auth.py
├── desktop.py
├── system.py
├── settings.py
├── maintenance.py
├── operations.py
├── assets.py
├── youtube.py
├── publishing.py
└── tasks.py
```

新路由优先进入对应 router/service，不要重新扩大单个 God file。

## 7. Subtitle Service

入口：

```text
src/videoroll/apps/subtitle_service/main.py
```

关键模块：

| 文件 | 职责 |
|---|---|
| `processing.py` | ASR、字幕、渲染主处理 |
| `translation_stage.py` | 翻译阶段协调 |
| `rag.py` | RAG / Agent research |
| `agent_runtime.py` | budget、tool policy、cancel、trace |
| `agent_skills.py` | Skill loader / selection |
| `translation_quality.py` | Translation Plan / validator |
| `translation_memory.py` | TM write / recall |
| `translation_checkpoint.py` | 翻译断点 |
| `translation_trace.py` | Agent / translation trace |
| `embeddings.py` | Embedding provider / device |
| `provider_rate_limit.py` | provider rate/concurrency gate |
| `render_queue_store.py` | 渲染队列 |
| `worker.py` | Celery tasks |

## 8. AI Client

统一 AI 请求逻辑位于：

```text
src/videoroll/ai/
```

业务代码不要重复实现：

- Retry-After；
- HTTP retry；
- SSE；
- native tools；
- usage parsing；
- token accounting；
- cost estimation；
- provider error mapping。

新增 provider 行为优先扩展统一 AI Client。

## 9. Agent Tool 规则

Tool 必须：

1. 注册到 `ToolRegistry`；
2. 定义 Pydantic input/output schema；
3. 通过 `AgentRuntime` / `ToolExecutor`；
4. 接受 budget、cancel、policy、rate-limit 控制。

禁止无 Runtime 直接执行 registry tool。

User Skill 不是可执行脚本插件，只能影响 prompt 和允许的工具集合。

## 10. 数据库

Model：

```text
src/videoroll/db/models.py
```

Migration：

```text
migrations/versions/
```

新增数据库字段时：

1. 修改 SQLAlchemy model；
2. 新建 Alembic migration；
3. 如需兼容 legacy schema，使用 schema compatibility helper；
4. 更新 PostgreSQL migration tests；
5. 更新相关 docs。

当前 head：

```text
0006_agent_runtime
```

运行：

```bash
DATABASE_URL=postgresql+psycopg://... \
python -m videoroll.db.migrate upgrade
```

## 11. PostgreSQL 与 pgvector

CI 使用：

```text
pgvector/pgvector:pg16
```

Embedding 数据允许不同模型和维度共存。

不要在代码里假设：

```text
embedding column == vector(当前配置维度)
```

查询必须考虑：

- `embedding_model`；
- `vector_dims`；
- target language；
- domain/status。

修改 ANN 查询或索引时，同时检查 `/subtitle/embedding/runtime` 的运行事实。

## 12. Durable Outbox

涉及外部副作用时，不应简单：

```python
db.commit()
celery_task.delay(...)
```

优先在同一 transaction 写：

- domain state；
- outbox event。

Worker 使用 operation inbox / operation key 防止重复消息造成重复外部副作用。

相关代码：

```text
src/videoroll/apps/outbox/
```

## 13. Realtime

Backend：

```text
src/videoroll/realtime.py
src/videoroll/apps/orchestrator_api/realtime.py
```

Frontend：

```text
src/web/src/lib/RealtimeProvider.tsx
src/web/src/lib/realtime.ts
```

实时 event 不是数据库替代品。页面初始状态必须来自 REST snapshot。

## 14. Frontend

主要页面：

- Dashboard；
- Tasks；
- Videos；
- Task Detail；
- YouTube Sources；
- Render Queue；
- Knowledge Base；
- Dictionary；
- Operations；
- Playout；
- Settings。

Settings 公共模式：

- `SettingsLayout`；
- `SettingsSaveBar`；
- `useUnsavedChangesGuard`。

可编辑配置页应避免：

- 改完直接切页丢失；
- 保存一个 section 时全页 refresh 覆盖其他草稿；
- 把数据库 key / env key 当主 UI label；
- 把危险操作混入普通保存按钮组。

## 15. Settings UI 原则

推荐：

```text
用户可读名称
  ↓
内部 key 作为次级辅助

配置值
  ≠
真实运行状态
```

例如 OpenVINO：

- 配置可以是 `GPU`；
- 运行状态必须额外检查 DRM 和 `ov.Core().available_devices`。

Embedding 同理：

- 配置维度不等于数据库列固定维度；
- UI 应显示 vector buckets 和实际 search mode。

## 16. 测试

全套：

```bash
python -m pytest -q
```

迁移专项：

```bash
python -m pytest -q \
  tests/test_database_initialization.py \
  tests/test_security_schema_migration.py \
  tests/test_database_initialization_postgresql.py
```

前端：

```bash
cd src/web
npm run lint
npm test
npm run build
```

Compose：

```bash
docker compose -f docker-compose.yml config --quiet
docker compose -f compose.yml config --quiet
docker compose -f fromprod/docker-compose.yml config --quiet
```

## 17. CI

`.github/workflows/ci.yml` 当前包含：

- Backend pytest；
- PostgreSQL + pgvector migration tests；
- Redis service；
- ffplayout gateway contract；
- security smoke；
- frontend ESLint；
- Vitest；
- frontend production build；
- Compose config；
- Docker core/subtitle dependency-boundary smoke。

Core image 不应泄漏重型 subtitle 依赖，例如：

- torch；
- sentence-transformers；
- optimum；
- faster-whisper；
- openvino-genai。

## 18. 日志与错误

原则：

- provider error 保留 status/error type；
- 不记录完整 API Key/Cookie；
- Agent trace 使用结构化 event；
- task log 正文由 storage 管理；
- WebSocket 只发 `log.updated`，不推整个日志；
- “unknown external side effect”不能自动当作“确定失败”。

## 19. 修改发布链

新增平台时同时评估：

- `Platform`；
- settings；
- account；
- PublishBatch target；
- worker queue；
- idempotency；
- outcome semantics；
- cleanup；
- UI；
- alerts。

外部发布超时可能代表平台已经完成副作用，不能机械自动重试。

## 20. 文档同步

以下改动必须同步文档：

- 新 service / port / network；
- route contract；
- migration；
- environment variable；
- production directory；
- security boundary；
- task status；
- Agent/translation pipeline。

## 21. 提交前检查

至少：

```bash
git diff --check
python -m pytest -q

cd src/web
npm run lint
npm test
npm run build
```

涉及 Compose：

```bash
docker compose -f docker-compose.yml config --quiet
```

涉及 migration：

```bash
python -m pytest -q tests/test_database_initialization_postgresql.py
```
