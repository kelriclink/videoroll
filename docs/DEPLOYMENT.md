# 部署指南

本指南适用于 Docker Compose 生产部署。生产状态保存在外部 PostgreSQL、`STORAGE_HOST_ROOT` 和项目的其他 `data/` 挂载中；升级镜像时保留它们。

## 1. 部署前准备

需要 Docker Engine、Docker Compose Plugin、PostgreSQL 16+ 和 pgvector：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

从 `.env.example` 创建私有 `.env`，并保留既有的 `DATABASE_URL`、端口和数据目录。生产必须设置随机、非空的：

```dotenv
DEVELOPMENT_MODE=false
STORAGE_HOST_ROOT=./data/storage
STORAGE_ROOT=/storage/objects
TMPDIR=/storage/.partial
INTERNAL_API_SECRET=...
ADMIN_BOOTSTRAP_SECRET=...
# The browser-facing playout hostname and URL.  Both must be set before the
# Web image is built.
PLAYOUT_HOST=playout.example.com
VITE_FFPLAYOUT_URL=https://playout.example.com
VIDEOROLL_PUBLIC_ORIGIN=https://app.example.com
LEGACY_LIVE_ENABLED=false
```

`.env`、数据库密码、平台 Cookie 和 Fernet 密钥不得提交到仓库或随工单传播。

## 2. 持久化目录

生产主机建议把以下目录放在容量充足的磁盘；升级时不要删除或用构建机的空目录覆盖：

```text
data/storage/            # 共享文件存储
data/models/             # Whisper / OpenVINO 模型
data/secrets/            # fernet.key 与运行密钥
data/redis/              # Redis AOF
```

容器以 `APP_UID:APP_GID` 运行。Compose 和导出脚本也把这两个值传给镜像构建，使镜像内的应用账号、社交发布日志目录和私有 HOME 使用相同的 UID/GID。未设置时二者均为 `10001`；`dev_up.sh` 首次生成配置时使用当前宿主机用户的 ID。已有挂载目录的属主应与这两个值匹配。

使用离线镜像和 `--no-build` 时，部署机必须保留构建时的 `APP_UID`、`APP_GID`。如果需要变更这两个值，先重新构建所有应用镜像并调整挂载目录的属主；仅修改部署机 `.env` 不会改变已有镜像内目录的所有权。

首次部署先创建共享目录并授权给容器用户。推荐直接运行幂等准备脚本：

```bash
sudo env ENV_FILE=.env ./scripts/prepare_prod_dirs.sh
```

如果部署用户本身就是 `.env` 中的 `APP_UID:APP_GID`，也可以不使用 `sudo`。脚本不会修改 `.env`。

它会创建并修正 `data/ffplayout/{db,logs,playlists,public}` 和
`${STORAGE_HOST_ROOT}/playout-media` 的属主。全新生产机必须在 `docker compose up`
之前执行；否则 Docker 以 root 自动创建 bind mount 目录时，uid 10001 的
ffplayout 可能无法创建 SQLite、Playlist 或 HLS 文件。

同一个 `STORAGE_HOST_ROOT` 必须挂载到 Orchestrator、字幕、Bilibili 和社交投稿的 API/worker 容器。不要分别挂载不同目录，否则数据库中的相对 key 会在部分服务中找不到。

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
- `videoroll-ffplayout:prod`
- `redis:7`

主应用与 egress 网关都默认使用 `INSTALL_ASR=1` 构建，安装完整本地 ASR 依赖，不再为网关单独关闭 ASR。安装依赖不会让网关自动加载语音模型。

根目录 `.dockerignore` 排除了 `social-auto-upload` 中的 Cookie、日志、本地配置与运行数据库；它们不会进入新构建的镜像。已生成的旧镜像和旧部署包不会因此被清理：如果曾在保存有账号登录状态的工作区构建过社交镜像，应停止分发旧包，重新构建，并在对应平台撤销其中可能包含的旧登录凭据。

### ffplayout 播控子系统

`compose.yml`、`docker-compose.yml` 和离线生产配套的 `fromprod/docker-compose.yml`
都包含 `ffplayout` 服务。它使用 upstream `main` 的固定 commit，镜像由
`services/ffplayout/Dockerfile.videoroll` 构建，Vue 前端嵌入 Rust binary。

ffplayout 仅加入 `internal` 网络，不发布宿主机端口；数据目录分别持久化到
`data/ffplayout/{db,logs,playlists,public}`，播控媒体位于
`${STORAGE_HOST_ROOT}/playout-media`。首次完成 ffplayout setup 时使用容器内路径：

```text
Logging:   /var/lib/ffplayout/logs
Playlists: /var/lib/ffplayout/playlists
Public:    /var/lib/ffplayout/public
Storage:   /var/lib/ffplayout/media
```

当前 VideoRoll 集成固定使用 **Channel 1**，并要求 **Shared Storage = false**。
不要修改 Channel 1 的 Storage 路径；VideoRoll importer 固定把成品写入
`/storage/playout-media/VideoRoll/<task-id>/`，该宿主目录在 ffplayout 容器内对应
`/var/lib/ffplayout/media`。多 Channel/动态 Storage 发现留给后续 API Adapter。

VideoRoll Orchestrator 同时将该宿主机目录作为 `/storage/playout-media` 访问。
任务详情中“加入播控”生成的媒体文件位于：

```text
/storage/playout-media/VideoRoll/<task-id>/<filename>
```

文件传输优先创建 hardlink；跨文件系统或权限不允许时才使用 `copy2`。映射关系
保存在 VideoRoll PostgreSQL 的 `playout_asset_links` 表中，ffplayout SQLite
不会写入 VideoRoll 字段。升级时必须同时保留 `data/storage/playout-media`，
并确保 Orchestrator 与 ffplayout 使用相同的 `STORAGE_HOST_ROOT`。

`PLAYOUT_HOST` 是 Web nginx 为 ffplayout 配置的专用虚拟主机名，
`VITE_FFPLAYOUT_URL` 是编译进 SPA 的浏览器访问地址。生产反向代理应将应用域名
和播控域名都转发到 Web 的唯一宿主机端口，并保留原始 `Host`，例如：

```dotenv
PLAYOUT_HOST=playout.example.com
VITE_FFPLAYOUT_URL=https://playout.example.com
VIDEOROLL_PUBLIC_ORIGIN=https://app.example.com
LEGACY_LIVE_ENABLED=false
```

修改 `VITE_FFPLAYOUT_URL` 后必须重新构建 Web 镜像；它不是运行时变量。浏览器
不得访问 Docker DNS 名称 `ffplayout:8787`，宿主机也不应发布 8787。

**首次 setup 不得直接暴露到公网。** ffplayout 尚未初始化时 `/api/setup` 无需
登录，因此上线顺序必须是：先启动容器并让 `PLAYOUT_HOST` 仅管理员 IP、临时
Basic Auth 或内网可访问；按上面的容器内路径完成 setup；确认
`GET /api/setup` 已返回 `required=false` 后，再移除临时访问限制并开放播控域名。
不要先公开域名再创建 Global Admin。

如果使用 Nginx、Caddy 或 Traefik 等外部反向代理，应用域名和
`PLAYOUT_HOST` 对应的域名都应指向同一个 Web 端口；TLS 在外部代理终止时，
`VITE_FFPLAYOUT_URL` 仍应使用浏览器实际访问的 `https://` 地址。

基础 Compose 不要求 GPU。使用 `docker-compose.intel.yml` 时，才会向
ffplayout 添加 `/dev/dri` 和 `INTEL_GPU_RENDER_GID`。生产离线导出脚本会额外
构建并导出 `videoroll-ffplayout:prod`；目标机仍使用 `docker load` 后的
`docker compose up -d --no-build`。

ffplayout runtime 安装 Intel media/VPL 用户态组件，但 `ffmpeg -encoders` 中出现
`h264_qsv`、`hevc_qsv`、`h264_vaapi` 或 `hevc_vaapi` 只表示编码器 wrapper 存在，
不代表宿主机 GPU 已可用。在有 `/dev/dri/renderD128` 的目标机上必须实际做一次
5–10 秒 QSV/VAAPI encode smoke test；失败时保持 CPU encoder 可用并排查设备组、
驱动和 runtime，不要把“encoder 名称存在”当作 GPU 验收通过。

将 tar、同名 `.sha256`、生产 `docker-compose.yml`、私有 `.env`，以及 `scripts/prepare_prod_dirs.sh` 传到目标机，并保持脚本位于目标部署目录的 `scripts/` 子目录。目标机不需要其余源代码；不要传输开发机的 `data/`。首次启动前先执行 `sudo env ENV_FILE=.env ./scripts/prepare_prod_dirs.sh`，再执行下面的 `docker load` / `docker compose up` 流程。

```bash
sha256sum -c videoroll-prod-bundle-<timestamp>.tar.sha256
docker load -i videoroll-prod-bundle-<timestamp>.tar
docker compose --env-file .env config -q
docker compose --env-file .env up -d --no-build --remove-orphans
docker compose --env-file .env ps
```

如果这是从 MinIO 切换到共享文件系统的已有数据库，先执行一次 dry-run：

```bash
docker compose --env-file .env run --rm orchestrator \
  python -m videoroll.storage.recovery
```

确认统计后再应用：

```bash
docker compose --env-file .env run --rm orchestrator \
  python -m videoroll.storage.recovery --apply
```

该修复会保留 `PUBLISHED` 和 `CANCELED` 历史；引用缺失对象的未完成 YouTube 任务回到 `INGESTED`，清除失效的资产、字幕、渲染和投稿作业，之后可重新下载。`PUBLISHING` 状态不会自动重置，以避免重复提交到平台。

`--no-build` 很重要：它确保目标机只使用已校验的离线镜像，而不在生产环境重新下载依赖。

## 4. 数据库迁移与运行验证

升级前备份数据库。导入镜像后、接收生产流量前执行：

```bash
docker compose --env-file .env run --rm orchestrator \
  python -m videoroll.db.migrate upgrade
docker compose --env-file .env up -d --no-build --remove-orphans
docker compose --env-file .env ps
```

迁移命令与各 API/worker 的启动初始化使用同一入口，支持空库、旧版库和曾由应用自动建表但尚无 Alembic 版本记录的库，也可重复执行。PostgreSQL 的结构变更在同一连接的 advisory lock 内串行完成，已有业务数据保留。

如果已有安全表的列类型、非空、主键或唯一约束不兼容，迁移会返回非零并阻止服务启动。按错误修复结构后重试；不要使用手工 `stamp head` 跳过验证。

确认 `web`、`orchestrator`、字幕 worker、publish worker、social worker、outbox dispatcher 和 egress gateway 均健康。

默认 Compose 设置 `TRUSTED_PROXY_HOSTS=web`，Orchestrator 只信任 Docker DNS 为该服务解析出的地址，按 `X-Forwarded-For` 中真实客户端的地址分别计算登录限流。DNS 信任缓存最多保留 60 秒；解析失败时忽略转发头。来自其他容器或不可信公网地址的转发头同样被忽略。若 web 前面还有受控反向代理，可通过 `TRUSTED_PROXY_CIDRS` 配置它的精确地址段，避免把整个 Docker 网段或所有来源设为可信。

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

Cloudflare Workers AI ASR 可以在 Web 的 ASR 设置中保存，也可以用生产 `.env` 提供默认值：

```dotenv
SUBTITLE_ASR_ENGINE=cloudflare-workers-ai
SUBTITLE_CLOUDFLARE_ACCOUNT_ID=<Cloudflare Account ID>
SUBTITLE_CLOUDFLARE_API_KEY=<Workers AI API Token>
SUBTITLE_CLOUDFLARE_MODEL=@cf/openai/whisper-large-v3-turbo
```

GroqCloud Whisper 使用专用 `groq-whisper` 引擎。它调用 Groq 的 OpenAI 风格音频接口，默认将音频按固定 45 秒编码为无损 FLAC 分片并保留 5 秒重叠，以减少代理上传量并限制单次推理时长；每片遇到网络断开、超时、429 或 5xx/524 时最多自动重试 5 次。成功分片会写入任务级检查点，继续字幕时会从失败分片恢复，最终将 segment 时间轴合并回原始音频：

```dotenv
SUBTITLE_ASR_ENGINE=groq-whisper
SUBTITLE_GROQ_WHISPER_API_KEY=gsk_...
SUBTITLE_GROQ_WHISPER_MODEL=whisper-large-v3-turbo
```

也可以在 Web 的 Settings · ASR 中保存并点击“测试 Groq ASR”。

API Token 不会通过设置接口回显；数据库内保存的 Token 使用 Fernet 加密，并优先于环境默认值。

## 6. 回退边界

可以停止 Web 流量、回退应用镜像或修复 Redis/worker 后让 outbox 重试；不能为了回退而恢复 query-token、未认证 noVNC、内部端口映射或绕过 egress gateway。数据库 schema 需要通过备份恢复回退，不能在事故现场随意降级。
