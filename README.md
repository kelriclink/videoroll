# videoroll

模块化视频处理流水线：内容接入 -> 字幕/翻译 -> 压制/封装 -> 可选投稿到哔哩哔哩。

详细规格见 [docs/PROJECT_SPEC.md](docs/PROJECT_SPEC.md)。

## 当前状态

- Web UI 已接入管理密码登录，首次打开需要设置 admin password。
- 后端容器会同时启动 `uvicorn`、字幕 Celery worker、投稿 Celery worker。
- YouTube 下载统一使用 `yt-dlp` 默认格式选择，不再额外强制 `mp4+m4a`。
- YouTube 支持在 Web UI 中配置代理、测试出口连通性、保存 `cookies.txt`，并显示 cookies 摘要。
- 若未配置 `YOUTUBE_COOKIE_FILE`，已保存到数据库的 YouTube cookies 会在下载/元信息提取时写入临时文件供 `yt-dlp` 使用。
- YouTube 触发风控时，接口会返回更明确的 cookies / proxy 提示。
- Bilibili 投稿支持在 Web UI 中保存 Cookie、测试登录、维护默认投稿模板。

## 架构概览

当前默认部署使用单容器应用服务：

- `app`
  运行 `videoroll.apps.monolith.main:app`
  挂载子服务：
  - `/api`
  - `/api/subtitle-service`
  - `/api/youtube-ingest`
  - `/api/bilibili-publisher`
  同时启动两个 Celery worker：
  - `subtitle`
  - `publish`
- `web`
  提供前端 UI，并将同源 `/api` 反代到后端
- `redis`
  Celery broker / backend
- `minio`
  资产存储
- `postgresql`
  需要你自行提供；当前仓库的 compose 文件不再内置 PostgreSQL

## 环境要求

- Docker Engine + Docker Compose Plugin
- 一个可用的 PostgreSQL 16+ 实例
- 访问 YouTube 所需的网络环境
- 如果要使用真实 ASR：
  `INSTALL_ASR=1`

可选但常见：

- Python 3.12+，用于本地运行辅助脚本
- Node.js 20+，用于单独启动前端开发服务器 `./scripts/dev_web.sh`

## 安装与启动

### 1. 克隆仓库

```bash
git clone git@github.com:kelriclink/videoroll.git
cd videoroll
```

### 2. 准备 PostgreSQL

当前 compose 不会帮你起数据库，你需要自己提供一个 PostgreSQL，并把它暴露给 `app` 容器。

一种简单方式是在宿主机单独跑一个 PostgreSQL：

```bash
docker run -d \
  --name videoroll-postgres \
  -e POSTGRES_USER=videoroll \
  -e POSTGRES_PASSWORD=videoroll \
  -e POSTGRES_DB=videoroll \
  -p 5432:5432 \
  postgres:16
```

### 3. 创建 `.env`

```bash
cp .env.example .env
```

至少确认这些变量：

- `DATABASE_URL`
- `REDIS_URL`
- `S3_*`
- `INSTALL_ASR`

如果数据库跑在宿主机，推荐这样配置：

```env
DATABASE_URL=postgresql+psycopg://videoroll:videoroll@host.docker.internal:5432/videoroll
REDIS_URL=redis://redis:6379/0
S3_ENDPOINT_URL=http://minio:9000
S3_ACCESS_KEY_ID=videoroll
S3_SECRET_ACCESS_KEY=videorollsecret
S3_BUCKET=videoroll
S3_REGION_NAME=us-east-1
S3_USE_SSL=false
```

说明：

- `app` 容器已注入 `host.docker.internal` 映射，因此 Linux/macOS/Windows 都可以按这个地址连接宿主机数据库。
- 如果你的 PostgreSQL 在别的主机或别的 Docker 网络里，把 `host.docker.internal` 改成实际地址即可。

### 4. 启动服务

推荐直接使用仓库脚本：

```bash
./scripts/dev_up.sh
```

如果你想手动启动，请显式指定 compose 文件，避免仓库里同时存在 `compose.yml` 和 `docker-compose.yml` 时的歧义：

```bash
docker compose -f docker-compose.yml --env-file .env up --build -d
```

### 5. 首次登录

打开 Web UI：

- `http://localhost:3000`

首次访问会进入 admin password 初始化页面：

- 第一次使用：设置管理密码
- 后续新设备或浏览器数据被清除后：输入管理密码登录

这是设备记住模式，密码不会每次都要求输入。

## 主要访问地址

- Web UI: `http://localhost:3000`
- Orchestrator API: `http://localhost:3000/api/docs`
- Subtitle Service: `http://localhost:3000/api/subtitle-service/docs`
- YouTube Ingest: `http://localhost:3000/api/youtube-ingest/docs`
- Bilibili Publisher: `http://localhost:3000/api/bilibili-publisher/docs`
- MinIO Console: `http://localhost:9001`

健康检查：

```bash
./scripts/dev_health.sh
```

查看日志：

```bash
./scripts/dev_logs.sh
```

停止服务：

```bash
./scripts/dev_down.sh
```

## 目录与持久化数据

- `data/minio`
  MinIO 数据目录
- `data/models`
  Whisper 模型目录
- `data/secrets`
  本地密钥和机密目录
- `data/secrets/fernet.key`
  首次保存加密配置时自动生成，用于加密存储 OpenAI key、YouTube cookies、Bilibili cookies 等敏感数据

不要随意删除 `data/secrets/fernet.key`，否则之前存储在数据库里的加密配置将无法解密。

## 配置说明

### 核心环境变量

- `DATABASE_URL`
  PostgreSQL 连接串
- `REDIS_URL`
  Redis 连接串
- `S3_ENDPOINT_URL`
  MinIO / S3 地址
- `S3_ACCESS_KEY_ID`
- `S3_SECRET_ACCESS_KEY`
- `S3_BUCKET`
- `S3_REGION_NAME`
- `S3_USE_SSL`
- `FFMPEG_PATH`
  默认 `ffmpeg`
- `WORK_DIR`
  默认 `/tmp/videoroll`
- `INSTALL_ASR`
  `1` 时构建真实 ASR 依赖；`0` 时减小镜像体积
- `PUBLISH_ADDR`
  Web / MinIO console 的监听地址，默认 `127.0.0.1`
- `WEB_PORT`
  Web 端口，默认 `3000`
- `MINIO_CONSOLE_PORT`
  MinIO console 端口，默认 `9001`

### YouTube 配置

推荐通过 Web UI 管理：

- `Settings -> YouTube`

支持的内容：

- 保存 / 清空代理
- 测试指定 URL 是否能通过当前代理访问
- 粘贴并保存 Netscape 格式的 `cookies.txt`
- 启用或禁用已保存 cookies
- 查看 cookies 是否包含登录态 / 是否可能包含 bot-check 豁免

当前默认行为：

- 下载直接使用 `yt-dlp` 默认格式选择
- 不再提供 `YOUTUBE_YTDLP_FORMAT`
- 默认 `User-Agent` 为浏览器 UA，而不是自定义短 UA

可选环境变量：

- `YOUTUBE_USER_AGENT`
- `YOUTUBE_COOKIE_FILE`
- `YOUTUBE_PROXY`
- `YOUTUBE_EXTRACTOR_ARGS_JSON`

说明：

- `YOUTUBE_COOKIE_FILE` 是可选的；如果不配，系统会优先使用你在 UI 中保存的 cookies。
- `YOUTUBE_EXTRACTOR_ARGS_JSON` 用于高级场景，例如：

```env
YOUTUBE_EXTRACTOR_ARGS_JSON={"youtube":{"player_client":["tv","android_sdkless","web"]}}
```

YouTube 风控注意事项：

- 浏览器导出 cookies 时，请使用和服务端相同的代理 / 出口 IP。
- 如果浏览器中出现过“确认你不是机器人”，通常需要在同一出口 IP 下先完成验证，再重新导出 cookies。
- 只有 `VISITOR_INFO1_LIVE` 一类 cookie 通常不够，建议确保 cookies 中包含登录态。
- YouTube cookies 轮换很快，过期后需要重新导出。

### ASR / 翻译 / 自动模式

相关页面：

- `Settings -> ASR`
- `Settings -> Translate`
- `Settings -> Auto`

说明：

- 默认 ASR 为 `faster-whisper`
- `Settings -> Auto` 用于配置 YouTube 自动模式的默认参数：
  - 字幕格式
  - 是否 burn-in / soft-sub
  - 编码参数
  - 翻译目标语言与 provider
  - 是否自动投稿 Bilibili

如果只是想做轻量演示，可把：

```env
INSTALL_ASR=0
SUBTITLE_ASR_ENGINE=mock
```

### Bilibili 配置

相关页面：

- `Settings -> Bilibili`

支持：

- 保存 Bilibili Cookie
- 检查是否成功解析出 `SESSDATA` 和 `bili_jct`
- 测试当前 Cookie 是否可登录
- 保存默认投稿模板 `default_meta`

真实投稿前，建议至少确认：

- Cookie 有效
- 含 `bili_jct`
- 默认模板里的分区、标签、简介符合你的账号需求

## 使用流程

### 本地视频

1. `New Task -> 本地上传`
2. 上传视频
3. 在任务详情页执行字幕、翻译、压制、投稿

### YouTube 手动模式

1. `New Task -> YouTube 链接`
2. 填写授权类型和可选证明链接
3. 创建任务
4. 在任务详情页手动执行：
   - 获取元信息
   - 下载 YouTube 视频
   - 生成字幕 / 翻译
   - 压制或投稿

### YouTube 自动模式

1. 先在 `Settings -> Auto` 配好默认参数
2. `New Task -> YouTube 自动模式`
3. 系统会按配置执行：
   - 下载
   - 字幕 / 翻译
   - burn-in 或 soft-sub
   - 可选自动投稿 Bilibili

## 常见问题

### 1. 启动后直接报数据库连接错误

优先检查：

- PostgreSQL 是否真的已经启动
- `.env` 里的 `DATABASE_URL` 是否指向了可达地址
- 如果数据库在宿主机，是否使用了 `host.docker.internal`

### 2. 打开页面后要求设置或输入密码

这是预期行为。所有非 `/auth/*` 和 `/health` 接口都受 admin 设备登录保护。

### 3. YouTube 下载报 `Sign in to confirm you're not a bot`

先检查：

- 当前网络 / 代理是否可稳定访问 YouTube
- 是否已经在 `Settings -> YouTube` 保存有效的 `cookies.txt`
- cookies 是否包含登录态
- 导出 cookies 时是否使用了同一代理 / 同一出口 IP

### 4. YouTube 下载报 `Requested format is not available`

当前代码已经统一改成 `yt-dlp` 默认格式选择；如果你还看到这个错误，通常不是项目手写格式规则的问题，而是：

- YouTube 当前返回的可用流本身异常
- cookies / 风控导致某些格式不可见
- `yt-dlp` / YouTube 抽取链路临时波动

优先更新 cookies、检查代理，再看是否需要调整 `YOUTUBE_EXTRACTOR_ARGS_JSON`。

### 5. OpenAI 配置存在哪里

OpenAI 配置通过 Web UI 保存到数据库，密钥内容用 `data/secrets/fernet.key` 加密。

## 前端开发模式

如果你只想本地调前端：

```bash
./scripts/dev_web.sh
```

这会在 `src/web` 下启动 Vite dev server。默认情况下，生产部署仍建议使用 `web` 容器。

## 合规边界

本项目仅用于处理你拥有版权、已获授权、或明确允许再分发的视频内容。不要将其用于批量搬运、绕过平台限制、或处理无授权内容。
