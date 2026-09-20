# VideoRoll

VideoRoll 是一个面向**已获授权视频**的模块化处理、字幕翻译与多平台发布系统。它把视频接入、ASR、RAG/Agent 研究、字幕翻译、渲染、审核、投稿、播控和运维监控放进同一条可恢复、可追踪的流水线。

当前文档基线：**2026-09-20**。

> 只处理你拥有版权、已获授权或明确允许再分发的内容。VideoRoll 不用于绕过平台限制、规避版权控制或批量搬运未授权内容。

## 核心能力

- **视频接入**：YouTube 单条 URL、频道/播放列表源扫描、首页推荐扫描、本地视频上传、Remote API。
- **ASR**：faster-whisper、OpenVINO GenAI、External Whisper-compatible、Groq Whisper、Cloudflare Workers AI。
- **Intel GPU**：OpenVINO ASR、Intel GPU 运行诊断，以及通过 /dev/dri 的相关硬件加速路径。
- **字幕翻译**：OpenAI-compatible / Cerebras-compatible 接口、批次摘要、thinking trace、断点续译。
- **RAG Agent**：术语发现、字典、Wikipedia、SearXNG、网页抓取、Agent Skills、工具策略、预算/并发/超时/取消。
- **翻译质量链**：Translation Plan、Translation Memory、术语/数字校验、失败 block 定向修复。
- **知识库与向量检索**：PostgreSQL + pgvector、Embedding 模型管理、批量重建、向量维度/HNSW 运行诊断。
- **字幕与渲染**：SRT、ASS、软字幕封装、硬字幕压制、渲染队列。
- **发布**：Bilibili，以及基于 social-auto-upload 的抖音、小红书、快手投稿。
- **播控**：集成 ffplayout，同源访问 /playout/，成品可直接加入播控媒体库。
- **运维**：AI Token/成本/错误统计、统一告警、系统资源、任务队列、Agent trace、发布状态与恢复。
- **可靠性**：durable outbox/inbox、幂等 operation key、lease/heartbeat、任务 stop/resume、translation checkpoint。
- **实时 UI**：WebSocket 推送增量状态；PostgreSQL 始终是最终状态源。

## 处理链

```text
YouTube / Local upload / Remote API
                 │
                 ▼
            Orchestrator
                 │
        ┌────────┴────────┐
        │                 │
        ▼                 ▼
  youtube-ingest     subtitle-service
                          │
                          ▼
                         ASR
                          │
                          ▼
              RAG / Agent research
                          │
                          ▼
                 Translation Plan
                          │
             ┌────────────┴────────────┐
             │                         │
             ▼                         ▼
      Translation Memory         RAG knowledge
             │                         │
             └────────────┬────────────┘
                          ▼
                    Draft translation
                          │
                          ▼
             Term / Number validation
                          │
                 failed blocks only
                          ▼
                    Targeted repair
                          │
                          ▼
                  SRT / ASS / Render
                          │
                  ┌───────┴────────┐
                  ▼                ▼
             Publish batch     ffplayout
```

## 运行架构

生产 Compose 当前包含 **15 个服务**：

| 类别 | 服务 |
|---|---|
| Web / API | `web`, `orchestrator` |
| 字幕 | `subtitle-service`, `subtitle-worker`, `subtitle-control-worker` |
| 接入 | `youtube-ingest` |
| Bilibili | `bilibili-publisher`, `publish-worker` |
| 社交平台 | `social-publisher-api`, `social-publisher-worker`, `social-publisher-scheduler` |
| 可靠性 | `outbox-dispatcher`, `redis` |
| 外网出口 | `egress-gateway` |
| 播控 | `ffplayout` |

浏览器默认只访问 Web：

```text
Browser
  │
  ▼
web / nginx
  ├── /api/*            ──► orchestrator
  ├── /api/ws/*         ──► orchestrator WebSocket
  ├── /playout/*        ──► ffplayout
  ├── /social-login/*   ──► social-publisher-api noVNC
  └── /social-publish/* ──► social-publisher-worker noVNC
```

生产默认只有 `web` 发布宿主机端口；内部 API 通过 Compose 网络通信。

详细说明见 [架构文档](docs/ARCHITECTURE.md)。

## 技术栈

| 层 | 技术 |
|---|---|
| Backend | Python 3.12, FastAPI, Uvicorn |
| Database | PostgreSQL, SQLAlchemy 2, Alembic, pgvector |
| Queue / realtime | Redis 7, Celery 5, Redis Pub/Sub |
| Frontend | React 18, TypeScript, Vite, Nginx |
| Media | FFmpeg, VAAPI, ffplayout |
| ASR | faster-whisper, OpenVINO GenAI, cloud Whisper providers |
| Embedding | sentence-transformers, optimum-intel, OpenAI-compatible embedding |
| Ingest | yt-dlp |
| Social publish | social-auto-upload, Chromium/Patchright |

## 快速开始

### 1. 克隆

```bash
git clone git@github.com:kelriclink/videoroll.git
cd videoroll
git submodule update --init --recursive
```

`social-auto-upload` 是构建 social-publisher 镜像所需的固定 Git submodule。

### 2. 准备 PostgreSQL

生产 Compose 不内置 PostgreSQL。请准备 PostgreSQL，并启用 pgvector。

从 [.env.example](.env.example) 开始配置：

```dotenv
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/videoroll
```

### 3. 启动开发环境

```bash
./scripts/dev_up.sh
```

首次运行时，如果 `.env` 不存在，脚本会：

- 从 `.env.example` 创建本地环境文件；
- 生成唯一的 `INTERNAL_API_SECRET`；
- 生成唯一的 `ADMIN_BOOTSTRAP_SECRET`；
- 写入当前 UID/GID；
- 创建必要的数据目录。

默认入口：

```text
Web UI   http://localhost:3000
API docs http://localhost:3000/api/docs
```

常用命令：

```bash
./scripts/dev_health.sh
./scripts/dev_logs.sh
./scripts/dev_down.sh
./scripts/dev_web.sh
```

## 首次配置

打开 Web 后先完成管理员初始化，然后从“设置”中配置：

1. **自动模式**：输出格式、字幕方式、ASR、翻译、自动投稿策略。
2. **YouTube**：代理、Cookies、兼容模式、首页扫描。
3. **投稿**：Bilibili Cookies、社交平台账号、默认投稿模板。
4. **ASR**：本地/云端引擎、模型、OpenVINO 设备。
5. **翻译 / RAG**：模型接口、Agent、Embedding、知识库。
6. **审核**：屏蔽词与 AI 审核规则。
7. **存储**：资源保留策略。
8. **API**：Remote API Token。

设置页支持未保存状态提示和离开保护，避免切换页面时误丢草稿。

## 翻译与 RAG

VideoRoll 的研究结果不是简单附加到 prompt 尾部，而是进入结构化质量链：

```text
Research
  ↓
Translation Plan
  ↓
terminology constraints + Translation Memory + context
  ↓
draft translation
  ↓
validation
  ↓
targeted repair for failed blocks
  ↓
final translation
```

Translation Plan 支持：

- `hard`：必须使用的术语；
- `preferred`：优先采用的研究译法；
- `contextual`：仅在对应上下文生效；
- block applicability、aliases、confidence、TM examples。

Translation Memory 当前信任边界：

- 同一个 Task 内可以召回机器生成的 TM；
- 跨 Task 只召回标记为 `approved` 的 memory。

## Embedding 与 pgvector

Embedding 页面显示真实运行状态，而不仅是配置值：

- Provider / Model / Device；
- 配置维度；
- pgvector 版本；
- 数据库列类型；
- 当前模型向量数量；
- 按模型和维度统计的向量分布；
- HNSW 是否存在、是否适用于当前查询；
- 当前实际使用 `HNSW`、`Exact scan` 或 `Unavailable`。

知识库允许历史模型、不同维度向量共存，因此数据库列可以是通用 `vector`。查询会显式按模型和 `vector_dims()` 过滤；没有匹配 ANN 索引时会回退到精确余弦扫描。

## Intel GPU / OpenVINO

生产环境统一使用：

```bash
./scripts/prod_compose.sh up -d --no-build --remove-orphans
```

`prod_compose.sh` 会检查：

- `docker-compose.intel.yml`；
- `/dev/dri/renderD128`；
- `INTEL_GPU_RENDER_GID`；
- `SUBTITLE_ASR_ENGINE`。

满足条件时自动合并 Intel override，避免容器重建后丢失 DRM 设备或 render group。

ASR 设置页还会显示 OpenVINO 实际枚举到的设备，用于区分：

```text
配置写了 GPU
≠
容器真的能使用 GPU
```

## 生产离线部署

构建完整离线镜像包：

```bash
git submodule update --init --recursive

ENV_FILE=/path/to/production.env \
INCLUDE_BASE_IMAGES=1 \
./scripts/build_export_prod.sh
```

目标机：

```bash
sha256sum -c videoroll-prod-bundle-*.tar.sha256
docker load -i videoroll-prod-bundle-*.tar

./scripts/prod_compose.sh config -q
./scripts/prod_compose.sh up -d --no-build --remove-orphans
./scripts/prod_compose.sh ps
```

完整目录、迁移、GPU、验证和回滚流程见 [部署指南](docs/DEPLOYMENT.md)。

## 数据与持久化

默认宿主机布局：

```text
data/
├── secrets/
├── models/
├── redis/
├── storage/
│   └── playout-media/
└── ffplayout/
    ├── db/
    ├── logs/
    ├── playlists/
    └── public/
```

PostgreSQL 是主要业务状态源。Redis 用于 Celery、实时事件和部分速率/并发辅助状态。

不要提交：

- `.env`；
- API Key / Cookie / Remote API Token；
- 数据库密码；
- `data/secrets/fernet.key`；
- 平台 storage_state；
- 用户素材或生产日志。

## 可靠性模型

VideoRoll 不把“发 Celery 消息”当成业务事务本身。

```text
DB transaction
   │
   ├── domain state
   └── outbox_events
           │
           ▼
    outbox-dispatcher
           │
           ▼
         Celery
           │
           ▼
   operation_inbox / worker lease
```

主要机制：

- durable outbox；
- operation inbox；
- operation key / idempotency；
- worker lease / heartbeat；
- task stop / resume；
- translation checkpoint；
- agent checkpoint + append-only trace；
- Remote API durable idempotency record。

## 开发与验证

后端：

```bash
python -m pytest -q
```

前端：

```bash
cd src/web
npm ci
npm run lint
npm test
npm run build
```

Compose：

```bash
docker compose -f docker-compose.yml config --quiet
docker compose -f compose.yml config --quiet
```

CI 会运行：

- Backend pytest；
- PostgreSQL + pgvector migration tests；
- ffplayout gateway contract；
- security smoke；
- frontend ESLint / Vitest / production build；
- Compose config；
- core/subtitle Docker dependency-boundary smoke。

## 文档

从 [docs/README.md](docs/README.md) 开始。

| 文档 | 内容 |
|---|---|
| [PROJECT_SPEC.md](docs/PROJECT_SPEC.md) | 当前产品、领域模型与流水线规格 |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | 服务边界、网络、可靠性、翻译/RAG 数据流 |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | 开发/生产部署、迁移、Intel GPU、回滚 |
| [DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md) | 代码结构、测试、扩展规则 |
| [REMOTE_API.md](docs/REMOTE_API.md) | Remote API 合约与幂等语义 |
| [realtime-events.md](docs/realtime-events.md) | WebSocket 实时事件 |
| [AGENT_SKILLS.md](docs/AGENT_SKILLS.md) | Agent Skill 格式与安全边界 |
| [social-publisher.md](docs/social-publisher.md) | 抖音/小红书/快手账号和投稿 |
| [SECURITY_AUDIT.md](docs/SECURITY_AUDIT.md) | 当前安全控制与剩余风险 |
| [REFERENCES.md](docs/REFERENCES.md) | 上游项目与依赖来源 |

## 合规说明

仓库中的第三方组件、子模块和集成项目各自遵循其上游许可证。部署者需要自行确认素材授权、平台条款、账号使用权限以及适用法律。
