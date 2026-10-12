# VideoRoll 原生部署

本方案在 Linux 上用独立 systemd units 运行 VideoRoll，统一入口为 `videorollctl`。运行时不需要 Docker；构建 Hatchet 时可以在构建机使用 Docker，只向服务器传原生二进制和静态资源。

状态：已有安装、启动、版本包、更新、回滚和离线测试实现；尚未在目标生产服务器完成整栈、GPU、浏览器投稿验证。旧 Compose 可保留用于首次切换回退。

## 1. 部署边界

- PostgreSQL 16+ 沿用独立部署；业务库与 Hatchet 库必须分开。
- Redis、Hatchet、各 API/worker、Web 分别由 systemd 管理。ffplayout 可选。
- 保留 core、subtitle、workflow、subtitle-workflow、social 五个 Python 环境，统一使用 Python 3.12；不会替换系统 Python。
- Hatchet SDK 使用 protobuf 6.33.6，与核心环境的 protobuf 7 隔离。
- Python、FFmpeg、Chromium、模型和二进制是运行环境，首次安装或专门升级时更新。
- 日常发布包只带应用源码、部署元数据和可选的前端产物，不携带模型、媒体、Cookie、`.env`、venv 或 Docker 镜像。
- 应用包暂不支持自动升级依赖、systemd 配置或数据库 schema；检测到这些变化会在停止服务前拒绝更新。

适用环境为单台 Linux systemd 主机。多机 worker 必须另外协调停收任务和版本，不应把本机检查当作分布式部署锁。

## 2. 目录

以代码初次克隆到 `/DATA/videoroll` 为例：

```text
/DATA/videoroll/                  # 首次安装的源码和 social-auto-upload
/DATA/videoroll/data/             # 共享媒体、模型、工作目录、状态，升级不搬移
/etc/videoroll/videoroll.env       # 配置和密钥，安装器不覆盖已有文件
/opt/videoroll/venvs/             # 五组 Python 3.12 运行环境
/opt/videoroll/current            # 当前应用版本的符号链接
/opt/videoroll/previous           # 上一个已验证的版本
/opt/videoroll/releases/          # 保留的应用版本和初始源码快照
/opt/videoroll/deployment.json    # 切换状态，用于识别中断的发布
```

每个 release 的 `data` 指向同一个共享目录，`social-auto-upload` 指向初次安装的固定运行依赖。前端位于 release 的 `web`；代码包未附前端时复用已安装的静态产物。

安装路径 `/opt/videoroll` 固定；仓库可以位于其他非 `/root`、非 `/home` 的位置。systemd 的 `ProtectHome` 会阻止服务读取这些家目录。不要删除初次安装的仓库、它的 data 或 social 子模块。

## 3. 首次安装

准备 Linux 主机、两个 PostgreSQL 数据库及可用软件源。安装脚本面向使用 apt 的 Debian/Ubuntu；完整 ffplayout 的 FFmpeg/libav 版本必须与构建机一致。建议 ffplayout 构建与运行使用同一发行版和 CPU 架构，不要直接复制不同发行版编译的程序。

```bash
git clone https://github.com/kelriclink/videoroll.git /DATA/videoroll
cd /DATA/videoroll
git submodule update --init --recursive
cp .env.example .env
```

在 `.env` 中填好 `DATABASE_URL`、`HATCHET_DATABASE_URL`、`INTERNAL_API_SECRET`、`ADMIN_BOOTSTRAP_SECRET`、`HATCHET_ADMIN_PASSWORD`，以及公开访问地址。Hatchet 密码应包含大小写字母和数字，长度 8–64。不要把真实配置提交到 Git。

准备三类构建产物：

```bash
# Web 构建环境使用 Node.js 22
bash scripts/native/build_web.sh --output-dir /tmp/videoroll-web

# Hatchet 在构建机使用 Docker，输出 bin/ 和 static-assets/
bash scripts/native/build_hatchet.sh --output-dir /tmp/hatchet-native

# ffplayout 构建环境使用 Node.js 24、Rust/Cargo，以及对应的 FFmpeg/libav 开发库
bash scripts/native/build_ffplayout.sh --output-dir /tmp/ffplayout-native
```

Web 与 ffplayout 可在不同构建环境运行，不要求生产服务器安装两个 Node 版本。将产物复制到目标机后：

```bash
cd /DATA/videoroll
sudo env \
  VIDEOROLL_WEB_DIST=/path/to/videoroll-web \
  HATCHET_ARTIFACT_DIR=/path/to/hatchet-native \
  FFPLAYOUT_ARTIFACT_DIR=/path/to/ffplayout-native \
  bash scripts/native/install.sh
```

不使用播控时省略 `FFPLAYOUT_ARTIFACT_DIR`。安装器安装系统包、五个 venv、Patchright Chromium、服务定义和指定产物，不启动业务服务。已有系统 Redis 不会被停掉；如占用 6379，应先安排端口或实例，避免与其他应用冲突。

若发行版没有 Python 3.12，安装器使用独立 uv 工具环境安装，不改系统 Python。可通过 `VIDEOROLL_PYTHON=/absolute/path/python3.12` 使用预先准备的解释器。首次安装需要能访问 apt/Python/浏览器下载源；应用版本包不等于全离线初装包。

检查生成的 `/etc/videoroll/videoroll.env`，随后：

```bash
sudo videorollctl doctor --database
sudo videorollctl start
sudo videorollctl enable
sudo videorollctl status
sudo videorollctl logs
```

首次 Orchestrator 启动会初始化业务 schema。Hatchet 先启动并完成自身迁移，token oneshot 成功后再启动业务服务。默认 Web 网关监听 `127.0.0.1:3001`，外层 Nginx/Caddy/OpenResty 负责 TLS 和对外入口。

`doctor` 检查环境文件、二进制、五个解释器、Web 产物、系统工具及可选数据库连通性。`health` 同时检查 HTTP 和 worker unit 存活；它不替代实际字幕/渲染/投稿烟测。

## 4. 日常小包更新

构建机打包已审查的工作区源码；该命令会包含当前源码改动，不自动提交或推送 Git：

```bash
# 只更新 Python 代码
python3 scripts/native/releases.py --output dist/videoroll-native.tar.gz

# 同时更新前端，先在 src/web 执行 npm ci && npm run build
python3 scripts/native/releases.py --output dist/videoroll-native-with-web.tar.gz --web-dist src/web/dist
```

也可手动运行 GitHub Actions 的 **Native application release**，下载应用包和 SHA256 文件。该工作流不连接生产服务器。

把包和校验文件传到服务器：

```bash
sha256sum -c videoroll-native.tar.gz.sha256
sudo videorollctl update --bundle /path/to/videoroll-native.tar.gz
sudo videorollctl update --bundle /path/to/videoroll-native.tar.gz --apply
```

默认仅预览。应用更新会：

1. 验证文件清单和 SHA256；拒绝路径穿越、链接、重复成员、缺文件和超限包。
2. 比较当前和目标运行环境、migration 输入；不兼容时拒绝。
3. 取得本机发布锁，将新版本展开到独立目录并编译检查 Python。
4. 为首次 Git 部署保存源码和前端快照，确保可以回到固定版本。
5. 只读检查排队/运行中的字幕、渲染、发布、pipeline、outbox/inbox 任务；忙时拒绝。
6. 停止应用 API、worker、Web，复查任务状态，再原子切换 current。
7. 启动全部应用服务，连续检查健康；失败时恢复旧版本并重新检查。

这是有短暂维护窗口的发布。当前版本统一重启应用服务，避免跨模块导入遗漏；Redis、Hatchet 和 ffplayout 不随代码发布重启。没有自动任务排空，也没有强制跳过忙碌检查的选项；请先暂停自动扫描/定时投稿及外部任务提交，等待已有任务完成。部署锁只协调本机发布，不锁住外部任务生产者。

每次发布都保留旧版本，暂不自动清理 releases。清理时不要删除 current、previous 或任何仍被 `web` 符号链接引用的目录。

## 5. 回滚

```bash
sudo videorollctl rollback
sudo videorollctl rollback --apply
```

原生回滚只切换应用版本，不改媒体、不清理凭据、不降级数据库。若发布被杀死或主机断电，`deployment.json` 中的 switching 状态会阻止下一次更新；先执行 rollback 恢复记录的旧版本，再检查服务和任务。

依赖或 schema 变化属于维护升级：停止接收任务、备份业务数据库及需要保留的文件、停服，在新的源码目录准备对应运行环境和前端/二进制，再显式执行迁移及验证。不要用普通小包发布绕过兼容性检查；旧依赖和数据库恢复需要独立安排，不承诺自动回退。

## 6. 从 Compose 切换

先完成原生安装和产物准备，对照现有挂载核实 native env 的媒体、模型、工作目录、ffplayout 状态路径以及 UID/GID。安装器不会替已有目录自动 chown，也不会迁移文件。切换应使用与旧部署一致的应用/schema 版本；如果包含 schema 变化，先按数据库维护升级处理。

暂停新任务并等待队列空闲，备份业务数据库和需要保留的媒体，然后：

```bash
sudo bash scripts/native/cutover.sh --execute
```

脚本先检查配置和任务状态，再停止旧 Compose、启动原生服务。失败时尝试停止原生服务并恢复旧 Compose。正常切换不执行 DB-only recovery，不清理文件索引、凭据或任务租约。

需要恢复旧 Compose 运行方式时：

```bash
sudo bash scripts/native/rollback.sh --execute
```

这与 `videorollctl rollback` 的“原生版本回退”不同；它不自动回退数据库迁移。

## 7. GPU、浏览器及生产验证

原生 GPU 使用宿主机驱动。安装器将 videoroll 加入已有 video/render 组，但不会替不同 GPU 安装驱动。Intel OpenVINO/VAAPI 需要匹配的 OpenCL/Level Zero/媒体驱动，NVIDIA 路径需要对应驱动及库。检查 `/dev/dri` 权限、应用硬件诊断和实际渲染，不能只依据设备文件存在判断可用。

Social API 和 worker 各有独立 Xvfb display；noVNC 只监听 loopback，继续通过带授权的 Web 网关访问。venv 只提供依赖隔离，不等同于容器网络隔离；内部服务保持 loopback，宿主机需要维持相应防火墙和服务账号权限。

生产验收至少覆盖：登录、上传、字幕翻译、实际 GPU/CPU 渲染、Social 登录桌面、经授权的投稿、ffplayout 播放，以及空闲状态下更新/故障回退。保存 `videorollctl status` 和相关日志。

## 8. 灾难恢复和旧实现

只有本地媒体已经丢失、明确接受终止旧任务时，才使用 `restore_from_database.sh --execute`。该工具会清理旧运行状态、失效文件索引及无法恢复的旧 Fernet 凭据，属于灾难恢复，不能当作正常部署入口。PostgreSQL 可恢复业务配置和记录，不能凭空恢复上传的媒体文件；需要保留的媒体应独立备份。

旧 `videoroll.service`/Supervisor 和 companion Compose 文件仅用于历史兼容，不由新安装器部署；不要与 `videoroll.target` 同时启用。旧 update.sh / update-code.sh 都转发到版本包更新入口，不再原地 git pull 或安装依赖。


## 9. 2026-10 原生运行架构与实机验收

原生化不是把全部 Compose 容器重新实现为软件包，更不是把 ASR、渲染与浏览器自动化合并进一个 Python 进程。VideoRoll 保留统一 Core 控制面、PostgreSQL 权威业务状态、Hatchet 持久化工作流、Redis/Celery 轻量任务执行；重型 ASR 与 Rust Render Node 是可独立运行的执行面。

### 最小依赖与可选执行节点

- 默认使用分布式 Rust Render Node。原生主机不再默认启动 `videoroll@render-worker.service`；需要本机 CPU/GPU 渲染兜底时执行 `sudo systemctl enable videoroll@render-worker.service`，再 `sudo systemctl start videoroll@render-worker.service`。`health.sh` 对已启用的本地渲染进程仍执行健康检查；更新或回滚不应意外启动原本停用的节点。
- 使用在线 faster-whisper、Groq 或 Cloudflare ASR 时，可在**首次安装**时设置 `VIDEOROLL_NATIVE_ASR_RUNTIME=remote`：两套字幕运行环境只安装基础服务依赖，不下载安装本地 Torch / OpenVINO / faster-whisper。必须另外在管理界面选择 `external-whisper`、`groq-whisper` 或 `cloudflare-workers-ai`；该选项不会自动修改数据库内已有的 ASR 设置。需要本地 ASR 或本地 Embedding 则必须使用默认 `local` 并提供相应运行时。
- 各环境依然按 Core、Workflow、Subtitle、Subtitle-Workflow、Social 隔离，避免 Hatchet SDK protobuf 6 与 Core 依赖冲突；这不是要求五个独立微服务必须跨机器部署。
- 系统配置目录 `/etc/videoroll` 为 `0711`，允许非特权服务通过目录读取 0644 的 nginx/Redis 配置，而 `videoroll.env` 必须是 root:root、0600，由 systemd 在降低进程权限前读取。安装器会对实际用户权限做检查。
- Python venv 必须位于 Linux 原生文件系统（例如 ext4），不要放在 WSL 的 `/mnt/c`、`/mnt/d`。安装器会拒绝常见非 POSIX 文件系统。发布构建与软件依赖编译应放到 CI/构建机；生产主机不构建 Hatchet。

### 真实运行验收（不是 mock）

需要安装完成、两套 PostgreSQL 数据库以及 Redis、Web、Core 已运行：

```bash
sudo bash scripts/native/smoke_runtime.sh core
# Hatchet、所有 HTTP API / Worker 都启动后，再运行：
sudo bash scripts/native/smoke_runtime.sh full
```

烟测进行双数据库 TCP 查询、以实际 `videoroll` 用户检查配置文件权限、Redis 临时键 SET/GET/DEL、SPA 页面和反代 API 实际 HTTP 200 与 JSON 校验。`full` 还检查 Hatchet 就绪状态和所有 Worker 单元。脚本不会生成字幕、渲染或真实投稿，也不能代替端到端媒体任务、浏览器登录以及远程 Rust Worker 的专项验收。

### 仍待解决的生产上线阻断项

- 目前数据库敏感字段的 `dbplain:v1:` 存储策略仍不具备静态加密保护；凭据加密与恢复密钥备份必须在正式生产迁移前完成。
- Hatchet 的公开 URL、Cookie Domain/Secure 策略需按实际 HTTPS 域名配置并进行真实登录测试；不能把 `localhost` 值原样用于公网访问。
- 业务数据库、媒体对象原件、Hatchet 状态、Cookie/浏览器状态的备份和升级回滚规则必须单独验证；数据库备份不等价于完整媒体备份。
- 当前发布工具仍不支持自动修改 schema 与运行环境依赖。本章节记录增量改造，不代表整个系统已经通过生产整栈验收。


### Hatchet 浏览器登录的公开 URL

首次安装前在 `.env` 设置实际公网 HTTPS 域名（下例仅为占位，不能直接使用）：

```ini
HATCHET_SERVER_URL=https://example.invalid
HATCHET_FRONTEND_URL=https://example.invalid/workflow-ui/
HATCHET_COOKIE_DOMAIN=example.invalid
HATCHET_COOKIE_INSECURE=false
```

Hatchet 浏览器 Cookie 会明确设置 Domain；不能依赖默认的 `localhost`。若只在本机调试，迁移器会把 Compose 的 `localhost:3000` 示例修正为 `127.0.0.1:3001`，但生产上线必须按实际域名覆盖。原生 `preflight.py` 会在启动前拒绝域名不匹配及 HTTPS 不安全 Cookie。完整公网验证仍需要测试真实登录、退出和 Nginx 代理。


### link 原生产数据目录（不搬迁）

link 的原有 Docker Compose 文件保留在 `/DATA/videoroll`，媒体和模型等持久数据位于 `/DATA/videoroll/data`。新源码可以使用 Git 放到 `/opt/videoroll/src`，安装时传入 `VIDEOROLL_DATA_ROOT=/DATA/videoroll/data`。安装器仅在新源码的 `data` 不存在时创建指向旧数据的软链接，检测到不同的现存数据目录会直接拒绝覆盖。此配置会将已有 Compose `data/storage/work`、`data/storage/state` 等路径映射到新的原生配置，不自动复制媒体，不修改原来的 PostgreSQL 卷。

```bash
# 在 link: /DATA/videoroll 保留原 Docker 配置和数据；代码独立检出
git clone https://github.com/kelriclink/videoroll.git /opt/videoroll/src
cd /opt/videoroll/src
git submodule update --init --recursive
sudo env VIDEOROLL_DATA_ROOT=/DATA/videoroll/data python3 scripts/native/migrate_env.py \
  --source /DATA/videoroll/.env --output /etc/videoroll/videoroll.env
# 原生 installer 仍需在启动前检查原始媒体/数据库和服务账户权限
sudo env VIDEOROLL_DATA_ROOT=/DATA/videoroll/data VIDEOROLL_NATIVE_ASR_RUNTIME=remote \
  bash scripts/native/install.sh
```

原生 Redis 使用隔离目录 `data/redis-native`，不直接操作原 Docker Redis 的 AOF 文件；旧 AOF 原样保留。切换前需确认旧 Redis 中没有必须恢复的未完成任务。预构建 Hatchet 二进制、ffplayout 及完整数据恢复仍须独立验收。


## 10. 项目内自带 Nginx/Redis 和集中配置（link）

2026-10 的 link 原生方案取消了 `/etc/videoroll`、`/usr/local/bin/hatchet-*`、`/usr/local/libexec/videoroll-*` 等散落位置：配置与密钥存于 `/opt/videoroll/config/videoroll.env`（root:root, 0600），Nginx/Redis 文本配置在同目录且由安装器按 `VIDEOROLL_DATA_ROOT` 渲染；应用二进制在 `/opt/videoroll/bin`，脚本在 `/opt/videoroll/libexec`，Hatchet/Nginx 的静态资源与动态库分别在 `share/` 与 `lib/`。系统唯一必要注册文件为 `/etc/systemd/system/videoroll*.service` 和 `videoroll.target`。

Nginx 和 Redis 是 Debian 13 包中的**原生二进制**，通过 `scripts/native/install_system_binaries.sh` 提取到项目目录，**不安装系统级 nginx/redis-server 服务**。为重复部署可在构建机提供已解压的 Debian 包目录 `VIDEOROLL_SYSTEM_BINARIES_ARTIFACT_DIR=/path/to/extracted`，其中包含 `usr/sbin/nginx`、`usr/bin/redis-server`、`usr/bin/redis-cli`、`etc/nginx/mime.types` 和 `usr/lib/*/liblzf.so.*`。不提供时安装脚本使用 `apt-get download` + `dpkg-deb -x`，无容器运行需求。仅适用与构建产物 ABI 兼容的目标 Debian/Ubuntu 系统，需要以 `ldd` 检验动态链接。

link 应将旧 Docker `.env` 作为迁移输入，**不是**直接修改旧环境文件：

```bash
sudo env VIDEOROLL_SOURCE_ENV=/DATA/videoroll/.env \
  VIDEOROLL_DATA_ROOT=/DATA/videoroll/data \
  VIDEOROLL_NATIVE_ASR_RUNTIME=remote \
  VIDEOROLL_SYSTEM_BINARIES_ARTIFACT_DIR=/DATA/videoroll/native-build-artifacts/system-services/extracted \
  VIDEOROLL_WEB_DIST=/DATA/videoroll/native-build-artifacts/web \
  HATCHET_ARTIFACT_DIR=/DATA/videoroll/native-build-artifacts/hatchet \
  FFPLAYOUT_ARTIFACT_DIR=/DATA/videoroll/native-build-artifacts/ffplayout \
  bash /opt/videoroll/src/scripts/native/install.sh
```

迁移后的 `VIDEOROLL_DATA_ROOT=/DATA/videoroll/data` 会供所有程序使用；`data/redis-native` 与旧 Docker AOF 隔离。`nginx.conf` 的 PID 和临时路径、Redis 的 dir 由同一值生成。源码目录 `src/data` 不再必须存在，也不需要成为旧数据的软链接。

`/opt/videoroll/config/videoroll.env` 仍含数据库凭据，应限制为 root 读取；服务由 systemd 在降权前加载变量。不要把该文件上传到 GitHub。

首次采用远程 ASR / 远程渲染时不强制安装 `fonts-noto-cjk`（软件包约 57 MB）；需要本机字幕渲染时可设置 `VIDEOROLL_INSTALL_CJK_FONTS=1`，或单独安装所需字体。


### 原有 ffplayout SQLite 路径迁移

从 Docker 切换到原生时，ffplayout 的 SQLite `config_global`、`channels`、`config_recording` 会保存 `/var/lib/ffplayout/` 的绝对路径；即使二进制命令行中设置 `--db`，仍会尝试创建该旧路径并报 `Permission denied`。不要因此直接修改 `/var/lib/ffplayout` 目录权限或初始化新数据库。

确认 ffplayout 已停止、旧 SQLite 已单独备份后执行：

```bash
sudo systemctl stop videoroll-ffplayout.service
sudo python3 /opt/videoroll/src/scripts/native/migrate_ffplayout_paths.py \
  --data-root /DATA/videoroll/data
sudo python3 /opt/videoroll/src/scripts/native/migrate_ffplayout_paths.py \
  --data-root /DATA/videoroll/data --execute
sudo systemctl start videoroll-ffplayout.service
```

此操作仅将已知目录配置项中的 `/var/lib/ffplayout/` 前缀替换为 `/DATA/videoroll/data/ffplayout/`；不会修改频道 ID、播放列表内容、媒体、账号或令牌。可以重复执行，第二次变更数量应为零。


### Node.js 离线前端发布产物

如果运行服务器不具备 Node 22 或 npm 网络不可用，开发机可使用 `npm ci && npm run build` 生成可验证的 `dist/`，将静态资源归档为版本化 `.tar.gz` 发布包。当前发布包 `deploy/artifacts/videoroll-web-73296ca.tar.gz` 对应 Git 源码版本 `73296ca`。运行服务器先校验 SHA-256，再解压至临时目录并调用 `scripts/native/build_web.sh --artifact-dir` 原子切换 `/opt/videoroll/web` 链接，不停止后端服务。该产物不含环境变量、数据库、Cookie、登录凭据或媒体。后续应由 CI 自动附加发布产物而不是长期累积在 Git 仓库中。


### 原生 OpenVINO GPU ASR 配置

对保留了 OpenVINO Whisper 模型、需要通过 Intel Arc A380 进行本地语音识别的 link，使用 VIDEOROLL_NATIVE_ASR_RUNTIME=openvino 安装字幕和字幕工作流环境。此模式仅安装基础程序及 OpenVINO GenAI 依赖，不强制安装额外的 Torch、Optimum Intel 或 Faster Whisper。为避免真实部署中遇到的 libopenvino.so.2640 缺失错误，固定 openvino==2026.4.0、openvino-tokenizers==2026.4.0.0 与 openvino-genai==2026.4.0.0，必须同时保持兼容版本。需在应用配置中将 subtitle.asr.default_model 设置为 /DATA/videoroll/data/models/whisper/whisper-large-v3-fp16-ov，并确保 videoroll 用户能够读取模型。该配置不会执行真实任务。模型 GPU 初始化、实际音频推理和每次新版本升级应另行验证。
