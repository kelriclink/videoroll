# 开发者指南

本文档面向需要在本地开发、调试或扩展 VideoRoll 的开发者。

## 仓库结构

```
src/videoroll/        Python 后端（FastAPI + Celery）
src/web/              React 18 + Vite + Tailwind 前端
apps/monolith         /api 编排入口
apps/subtitle_service ASR、翻译、压制队列、RAG Agent
apps/youtube_ingest   YouTube 接入
apps/bilibili_publisher  B 站投稿
config.py             共享配置（pydantic-settings）
db/                   数据库模型 & auto-migration
storage/              S3/MinIO 封装
ai/                   OpenAI 客户端（翻译、typeid 推荐、内容审核）
utils/                加密、内部 token 等共享工具
tests/                后端测试（pytest）
docs/                 本文档
scripts/              开发与部署脚本
Dockerfile            后端镜像
src/web/Dockerfile    前端镜像
deploy_compose/       生产 compose 配置
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
# 构建生产镜像并导出 tar 包
bash scripts/build_export_prod.sh

# 在目标机器导入
docker load -i videoroll-prod-bundle-*.tar
```

详细 Docker 部署见 [README.md](../README.md)。

## 架构要点

### Monolith 组装

四个 FastAPI 应用运行在同一个 uvicorn 进程：

| 挂载路径 | 模块 |
|---|---|
| `/api` | Orchestrator — 任务 CRUD、资产、认证、YouTube 下载、投稿元数据、设置 |
| `/api/subtitle-service` | Subtitle Service — ASR、翻译、压制队列、自动模式 |
| `/api/youtube-ingest` | YouTube Ingest — 源管理、频道/播放列表扫描 |
| `/api/bilibili-publisher` | Bilibili Publisher — 投稿、typeid 推荐 |

入口 `docker/entrypoint.sh` 同时运行 uvicorn + 2 个 Celery worker（subtitle 队列、publish 队列）。

### 任务状态机

```
CREATED → INGESTED → DOWNLOADED → AUDIO_EXTRACTED → ASR_DONE → TRANSLATED
→ SUBTITLE_READY → RENDERED → READY_FOR_REVIEW → APPROVED → PUBLISHING → PUBLISHED
```

服务间仅通过 DB 任务状态 + S3 存储 key 通信，不直接传递数据。

### Celery Workers

| 应用 | 队列 | 关键任务 |
|---|---|---|
| `subtitle_service.worker` | `subtitle` | `task_queue_tick`（调度器）、`process_job`（ASR+翻译）、`process_render_job`（ffmpeg 压制）、`auto_youtube_pipeline`、`after_render_publish`、`cleanup_task` |
| `bilibili_publisher.worker` | `publish` | `process_job`（上传 B 站） |

字幕队列使用任务级锁（`Task.lock_owner` / `Task.lock_until`）+ 可配置 `max_concurrency`。

### 数据库

PostgreSQL + psycopg 3。SQLAlchemy 2 ORM。无 Alembic，使用轻量 auto-migration（`db/auto_migrate.py`）在启动时 `ALTER TABLE ADD COLUMN`。

### 配置

每个服务一个 pydantic-settings `Settings` 类，继承 `CommonSettings`。环境变量驱动，支持 `.env` 文件，`@lru_cache` 缓存。

### 前端

React 18 + TypeScript + Vite + Tailwind + react-router-dom v6。生产环境 nginx 托管 SPA 并反代 `/api/`。开发时 Vite 反代 `/api` 到 `localhost:8000`。

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
