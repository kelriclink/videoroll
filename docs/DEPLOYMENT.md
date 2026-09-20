# VideoRoll 部署指南

当前基线：2026-09-20。

本文覆盖 Docker Compose 开发环境、离线生产部署、数据库迁移、Intel GPU、上线验证和回滚。

## 1. 推荐部署模型

```text
Internet / LAN / VPN
       │
       ▼
Outer Caddy / Nginx / TLS
       │
       ▼
127.0.0.1:3001
       │
       ▼
VideoRoll web/nginx
```

生产只公开 Web。不要把 Orchestrator、subtitle-service、publisher、Redis 或 ffplayout 内部端口直接公开到公网。

## 2. 前置条件

### 构建机

- Docker Engine；
- Docker Compose plugin；
- Git；
- Git submodule；
- 足够的镜像构建空间。

### 生产机

- Linux + Docker；
- Docker Compose plugin；
- 外部 PostgreSQL；
- 可写持久化目录；
- 如使用 Intel GPU：可见 `/dev/dri/renderD128`。

推荐数据库：

- PostgreSQL 16；
- pgvector extension。

## 3. 获取代码

```bash
git clone git@github.com:kelriclink/videoroll.git
cd videoroll
git submodule update --init --recursive
```

缺少 `social-auto-upload` submodule 时，开发和生产镜像构建脚本都会直接失败。

## 4. 环境文件

从：

```bash
cp .env.example .env
```

开始。

生产至少确认：

```dotenv
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/videoroll
INTERNAL_API_SECRET=CHANGE_ME
ADMIN_BOOTSTRAP_SECRET=CHANGE_ME

PUBLISH_ADDR=127.0.0.1
WEB_PORT=3001

STORAGE_HOST_ROOT=./data/storage
STORAGE_ROOT=/storage/objects
```

### 必须保护的变量

- `DATABASE_URL`；
- `INTERNAL_API_SECRET`；
- `ADMIN_BOOTSTRAP_SECRET`；
- LLM / ASR API Key；
- 平台 Cookie / account state；
- Remote API Token。

生产 `.env` 不应提交到 Git。

## 5. 持久化目录

标准布局：

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

准备目录：

```bash
sudo env ENV_FILE=.env ./scripts/prepare_prod_dirs.sh
```

### 发布时不可覆盖的数据

- `data/secrets`；
- `data/models`；
- `data/redis`；
- `data/storage`；
- `data/ffplayout`；
- 外部 PostgreSQL 数据。

尤其不要删除或替换：

```text
data/secrets/fernet.key
```

否则数据库中已加密的社交账号状态、Cookie 等可能无法解密。

## 6. 开发环境启动

```bash
./scripts/dev_up.sh
```

脚本明确使用：

```text
docker-compose.yml
```

而不是依赖 Docker Compose 在 `compose.yml` 与 `docker-compose.yml` 之间自动选择。

辅助：

```bash
./scripts/dev_health.sh
./scripts/dev_logs.sh
./scripts/dev_down.sh
./scripts/dev_web.sh
```

## 7. 生产 Compose 入口

生产统一通过：

```bash
./scripts/prod_compose.sh ...
```

例如：

```bash
./scripts/prod_compose.sh config -q
./scripts/prod_compose.sh up -d --no-build --remove-orphans
./scripts/prod_compose.sh ps
./scripts/prod_compose.sh logs -f orchestrator
```

在 Intel GPU 主机上不要随意替换为裸：

```bash
docker compose up
```

否则可能漏掉 `docker-compose.intel.yml`，导致重建后的容器看不到 `/dev/dri`。

## 8. Intel GPU 自动检测

`prod_compose.sh` 默认：

```text
VIDEOROLL_INTEL_GPU_COMPOSE=auto
```

它读取：

- `INTEL_GPU_RENDER_DEVICE`；
- `INTEL_GPU_RENDER_GID`；
- `SUBTITLE_ASR_ENGINE`。

自动加入 Intel override 的条件：

1. `docker-compose.intel.yml` 存在；
2. render device 存在；
3. ASR engine 为 `openvino`，或显式配置 render GID。

如果配置 GID 与宿主机设备实际 GID 不一致，脚本会拒绝启动。

### 宿主机检查

```bash
ls -l /dev/dri
stat -c '%g' /dev/dri/renderD128
```

### 容器检查

```bash
./scripts/prod_compose.sh exec subtitle-service \
  test -r /dev/dri/renderD128
```

OpenVINO：

```bash
./scripts/prod_compose.sh exec subtitle-service \
  python -c 'import openvino as ov; print(ov.Core().available_devices)'
```

Web 的 ASR 设置页还会显示：

- DRM device；
- driver / PCI；
- OpenVINO devices；
- OpenVINO GPU 实际可用状态。

## 9. 构建离线生产包

```bash
ENV_FILE=/path/to/production.env \
INCLUDE_BASE_IMAGES=1 \
./scripts/build_export_prod.sh
```

默认构建：

- `videoroll:prod`；
- `videoroll-subtitle:prod`；
- `videoroll-egress:prod`；
- `videoroll-web:prod`；
- `videoroll-social-publisher:prod`；
- `videoroll-ffplayout:prod`；
- 可选 `redis:7`。

输出：

```text
videoroll-prod-bundle-YYYYMMDD-HHMMSS.tar
videoroll-prod-bundle-YYYYMMDD-HHMMSS.tar.sha256
```

## 10. 离线导入

目标机：

```bash
sha256sum -c videoroll-prod-bundle-*.tar.sha256
docker load -i videoroll-prod-bundle-*.tar

./scripts/prod_compose.sh config -q
```

不要在加载新镜像前删除旧镜像；保留 rollback tag 更安全。

## 11. 数据库迁移

当前 migration head：

```text
0006_agent_runtime
```

推荐在切换业务容器前显式运行：

```bash
./scripts/prod_compose.sh run --rm orchestrator \
  python -m videoroll.db.migrate upgrade
```

然后再：

```bash
./scripts/prod_compose.sh up -d --no-build --remove-orphans
```

Orchestrator 自身启动也会调用 `initialize_database()` 并升级到 head；显式预迁移的价值是让 schema 错误在重建全部服务之前暴露。

Migration 错误会阻止服务正常启动，不应忽略。

## 12. 推荐滚动发布顺序

如果不能一次性停止全部服务，推荐：

1. 构建/加载新镜像；
2. 给旧镜像打 rollback tag；
3. 显式数据库 migration；
4. subtitle-service / subtitle-worker / subtitle-control-worker；
5. youtube-ingest / Bilibili / outbox 等 core 服务；
6. orchestrator；
7. social-publisher API / worker / scheduler；
8. 最后 Web。

最后切 Web 的原因：

- Nginx 持有内部 upstream；
- 后端容器重建会改变 Docker IP；
- 先稳定后端再重建 Web，入口行为更可预测。

## 13. 应用镜像版本一致性

以下镜像应被视为同一应用 release：

- core；
- subtitle；
- web；
- social-publisher；
- egress；
- ffplayout。

尤其是所有会运行数据库 initialization 的镜像必须包含与数据库 head 一致的 Alembic revisions。

典型错误：

```text
数据库已升级到 0006
但 social-publisher 还是只认识 0005
→ 服务启动时 Can't locate revision
```

因此不要只更新 Orchestrator 而长期保留旧 social 镜像。

## 14. Web 与同源入口

典型生产：

```dotenv
PUBLISH_ADDR=127.0.0.1
WEB_PORT=3001
VITE_FFPLAYOUT_URL=
```

ffplayout 默认通过：

```text
/playout/
```

访问。

外层 Caddy/Nginx 只需要反代 VideoRoll Web 端口。

不要额外公开 ffplayout `8787`。

## 15. Social Publisher

Web 通过内部 DNS 使用：

- `social-publisher-api`；
- `social-publisher-worker`。

noVNC 由 Web Nginx 反代，并使用短期 Desktop Grant 验证。

不要映射 6080 到公网。

## 16. 上线验证

### Compose

```bash
./scripts/prod_compose.sh ps
```

长期服务最终应全部进入 `healthy`。

### HTTP

```bash
curl -fsS http://127.0.0.1:3001/api/health
```

预期：

```json
{"status":"ok"}
```

SPA：

```bash
curl -I http://127.0.0.1:3001/
curl -I http://127.0.0.1:3001/settings/translate
```

### Logs

```bash
./scripts/prod_compose.sh logs --since 10m \
  orchestrator subtitle-service subtitle-worker web
```

重点检查：

- Alembic/migration failure；
- Traceback / CRITICAL；
- PostgreSQL / Redis connection；
- OpenVINO device；
- `/dev/dri` permission；
- Nginx upstream/DNS；
- external provider 401/402/429/5xx。

## 17. Embedding / pgvector 验证

Web：

```text
Settings → 翻译 / RAG → Embedding
```

运行状态来自：

```text
GET /subtitle/embedding/runtime
```

显示：

- pgvector version；
- column type；
- vector dimension buckets；
- active vectors；
- HNSW status；
- search mode。

`Exact scan` 本身不代表数据错误，只表示当前查询没有使用匹配的 ANN 索引。

## 18. ffplayout

ffplayout 状态：

```text
data/ffplayout/
```

播控媒体：

```text
data/storage/playout-media/
```

VideoRoll 的 `video_final` 由服务端 hardlink/copy 加入播控，不经过浏览器重新上传。

## 19. 备份

至少备份：

1. PostgreSQL；
2. `data/secrets`；
3. `data/storage`；
4. `data/ffplayout`；
5. 生产 `.env`。

`data/models` 可以重新下载时优先级可略低，但大模型的重新获取成本通常很高。

## 20. 回滚

发布前建议：

```bash
docker tag videoroll:prod videoroll:pre-release-TIMESTAMP
docker tag videoroll-subtitle:prod videoroll-subtitle:pre-release-TIMESTAMP
docker tag videoroll-web:prod videoroll-web:pre-release-TIMESTAMP
docker tag videoroll-social-publisher:prod videoroll-social-publisher:pre-release-TIMESTAMP
```

应用镜像回滚前必须确认数据库 migration 向后兼容。

不要因为应用回滚就自动执行 Alembic downgrade。数据 migration 回退必须独立评估。

## 21. 故障排查顺序

推荐：

```text
compose ps
   ↓
service health
   ↓
migration / PostgreSQL
   ↓
Redis / Celery
   ↓
Docker network / DNS
   ↓
GPU / device permissions
   ↓
application logs
   ↓
external provider
```

不要第一步就删除 volume、数据库、Redis 数据或生产素材。
