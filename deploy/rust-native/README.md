# 独立 Rust 部署候选配置

不依赖 Python VideoRoll、Celery、Hatchet 或 Redis 调度。
PostgreSQL/pgvector 沿用原库、原表。旧版部署目录及 unit 保留；
不提供自动停止旧服务或改生产库的命令。

## 进程与端口（默认全部回环）

| 角色 | 程序 | 端口 |
|---|---|---|
| Orchestrator/API | videoroll-v2 api | 18080 |
| 工作流、Outbox、扫描、GC | videoroll-v2 worker | 无 |
| Bilibili | videoroll-v2 service bilibili-publisher | 18082 |
| Subtitle | videoroll-v2 service subtitle-service | 18083 |
| YouTube | videoroll-v2 service youtube-ingest | 18084 |
| Egress | videoroll-egress | 18086 |
| SAU 控制及原生消费者 | videoroll-sau | 18087 |
| Web | Nginx | 13001 |

Worker 等待内部服务健康检查后才领取；Web 等待 API readiness。
默认不开启自动扫描和真实平台投稿。原浏览器 /api/* 映射到 /api/v1/*；
原 /api/v1/*、/api/v2/*、/render-workers/v1/* 保留 URI。
原 Hatchet iframe 替换为原生工作流诊断入口。
外部 TLS 反代与访问控制需独立配置；内部 API/noVNC 不直接暴露公网。

## 第三方边界

保留 yt-dlp/aria2c、FFmpeg/FFprobe、远程 RenderWorker。SAU 保留
Python/Playwright 浏览器引擎，账号检查、登录、任务执行控制与持久
投递由 Rust 管理；启用前独立配置 CLI、X11/noVNC/websockify 和 cookie
目录，仍使用 scoped grant 的 auth_request。保留 YouDub 的 Python
模型运行时、桥接、VoxCPM2/Demucs，准备完整上游子模块及模型依赖，
不与活跃旧 runtime 共用。保留 ffplayout 服务，原 loopback 8787
反代与媒体导入路径。无需强制将第三方模型/浏览器引擎改写为 Rust。

## 构建与组装

正式发布使用受支持的 Node 22 和 lockfile 安装依赖，运行 build-web.sh。
Rust 原生历史 Fernet 认证解密依赖系统 OpenSSL 3 libcrypto；构建需
libssl-dev，部署需对应 libcrypto.so.3。原密钥仅在配置绝对路径后读取，
必须属于 videoroll 服务用户、权限 0600，不复制或写回生产历史密文。
Cargo 使用 cargo build --release --locked --bins；CARGO_TARGET_DIR 可设到
空间充足的磁盘。stage-release.sh 接受新绝对目录、Cargo 二进制目录、
Nginx 二进制与 mime.types，生成源码版本及 SHA256SUMS，不安装、不启动。
应从干净提交构建，确保源码版本与产物一致；YouDub 子模块完整性需核实。
候选 release 放入 /opt/videoroll-rust/releases/<revision>，
审核后由运维设置 /opt/videoroll-rust/current。config/videoroll.env
按模板填写实际数据库/存储/密钥，root-owned 0600，不提交密钥。
创建专用 videoroll 用户/组，确认文件访问权限，复制 nginx.conf
和 systemd/ 下 units 及 worker drop-in。不要覆盖旧 target。
Nginx/MIME 包含在候选 release；系统依赖及 FFmpeg 仍需预先配置。

## 首次切换与日常启动

备份数据库/媒体、保留旧程序与配置；暂停所有旧、新任务生产者、消费者、
远程执行器并核查未完成外部操作。Rust migrate 必须显式执行，不在
systemd 启动时自动改库。通过 task-ownership-audit/adopt 与
outbox-audit/adopt 完成历史迁移。
在同一 native-only 环境下手动运行 standalone-preflight（无 --startup），
校验 migration 版本/checksum、历史所有权、旧工作流、投递账本、未知投稿、
存储目录与内部密钥配置。此检查只读，不证明真实平台回执或外部进程状态。
实际生产还需真实模型、平台和 GPU 测试；之后由运维启动独立 target。
systemd preflight 检查已知旧业务 unit、路径权限、执行文件，
运行 standalone-preflight --startup。日常重启时 Rust 未知投稿保持
隔离，不阻断全局恢复；旧所有权及旧投递仍阻止启动。
不要以 --startup 替代首次切换验收。无法自动探测所有手工启动、
容器或远程旧消费者。回滚前需停止 Rust 并核对外部副作用，依据验证过的
数据库恢复/逆向切换方案操作，不能直接启动旧队列导致重复投稿。

## 验证

VIDEOROLL_V2_TEST_NATIVE_DEPLOYMENT_ONLY=true bash rust/scripts/smoke-isolated.sh

仅临时 PostgreSQL 与本地进程；实际启动七个 Rust 角色及 Nginx，
检查旧浏览器 API、V1/V2、Worker 协议、桌面授权、静态页面和切换预检。
测试禁用真实模型与投稿；未安装 systemd 单元或绑定生产端口。
