# 部署指南

本指南适用于 Docker Compose 生产部署。生产状态保存在外部 PostgreSQL 与项目根目录的 `data/` 挂载中；升级镜像时保留它们。

## 1. 部署前准备

需要 Docker Engine、Docker Compose Plugin、PostgreSQL 16+ 和 pgvector：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

从 `.env.example` 创建私有 `.env`，并保留既有的 `DATABASE_URL`、端口和数据目录。生产必须设置随机、非空的：

```dotenv
DEVELOPMENT_MODE=false
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
INTERNAL_API_SECRET=...
ADMIN_BOOTSTRAP_SECRET=...
```

`.env`、数据库密码、平台 Cookie 和 Fernet 密钥不得提交到仓库或随工单传播。

## 2. 持久化目录

项目根目录保留以下目录；升级时不要删除或用构建机的空目录覆盖：

```text
data/minio/              # 对象存储数据
data/models/             # Whisper / OpenVINO 模型
data/work/               # 可恢复的本地工作区
data/secrets/            # fernet.key 与运行密钥
data/social-publisher/   # 社交投稿工作目录
data/redis/              # Redis AOF
```

容器以 `APP_UID:APP_GID` 运行。已有挂载目录的属主应与这两个值匹配。

## 3. 离线镜像包

在可联网的构建机执行：

```bash
git submodule update --init --recursive
ENV_FILE=/secure/path/production.env INCLUDE_BASE_IMAGES=1 ./scripts/build_export_prod.sh
```

生成的 `videoroll-prod-bundle-<timestamp>.tar` 同时包含：

- `videoroll:prod`（Orchestrator、内部 API、worker、dispatcher）
- `videoroll-egress:prod`
- `videoroll-web:prod`
- `videoroll-social-publisher:prod`
- `redis:7`、`minio/minio:latest`、`minio/mc:latest`

将 tar、同名 `.sha256`、生产 `docker-compose.yml` 和私有 `.env` 传到目标机。目标机不需要源代码；不要传输开发机的 `data/`。

```bash
sha256sum -c videoroll-prod-bundle-<timestamp>.tar.sha256
docker load -i videoroll-prod-bundle-<timestamp>.tar
docker compose --env-file .env config -q
docker compose --env-file .env up -d --no-build
docker compose --env-file .env ps
```

`--no-build` 很重要：它确保目标机只使用已校验的离线镜像，而不在生产环境重新下载依赖。

## 4. 数据库迁移与运行验证

升级前备份数据库。导入镜像后、接收生产流量前执行：

```bash
docker compose --env-file .env run --rm orchestrator \
  python -m videoroll.db.migrate upgrade
docker compose --env-file .env up -d --no-build
docker compose --env-file .env ps
```

确认 `web`、`orchestrator`、字幕 worker、publish worker、social worker、outbox dispatcher 和 egress gateway 均健康。`minio-init` 显示已成功退出是正常状态。

观察 outbox pending 年龄、lease 恢复、内部 token 拒绝和 egress 拒绝日志。不要直接删除 `outbox_events`、`operation_inbox` 或状态不明的发布记录。

## 5. Intel iGPU OpenVINO ASR

Intel GPU 需要同时具备宿主机设备映射、容器组权限和 OpenVINO 配置。先在目标机确认：

```bash
test -e /dev/dri/renderD128
stat -c '%g' /dev/dri/renderD128
```

在生产 Compose 的 `subtitle-service` 与 `subtitle-worker` 中加入：

```yaml
devices:
  - "/dev/dri:/dev/dri"
group_add:
  - "${INTEL_GPU_RENDER_GID}"
```

生产 `.env` 设为：

```dotenv
SUBTITLE_ASR_ENGINE=openvino
SUBTITLE_OPENVINO_DEVICE=GPU
INTEL_GPU_RENDER_DEVICE=/dev/dri/renderD128
INTEL_GPU_RENDER_GID=<上一步 stat 输出的数字>
```

仓库提供 `docker-compose.intel.yml` 作为标准覆盖层；专用生产 Compose 也可以把上述内容直接合并。OpenVINO Whisper 模型放在 `data/models/whisper/`；若已有数据库内的 ASR 设置，它会优先于环境默认值，因此还需在 Web 的 ASR 设置中确认引擎为 `openvino`、设备为 `GPU`。

部署后检查：

```bash
docker compose --env-file .env exec subtitle-worker \
  test -r /dev/dri/renderD128
```

## 6. 回退边界

可以停止 Web 流量、回退应用镜像或修复 Redis/worker 后让 outbox 重试；不能为了回退而恢复 query-token、未认证 noVNC、内部端口映射或绕过 egress gateway。数据库 schema 需要通过备份恢复回退，不能在事故现场随意降级。
