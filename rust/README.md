# VideoRoll 2.0 Rust 重写项目

当前状态：独立实验分支 rewrite/rust-v2。不能直接替换生产版。
原版主分支和部署不变，旧 PostgreSQL 表只用于兼容读取、明确的任务与资产同步；新工作流表以 vr2_ 为前缀。

## 功能范围

| 子系统 | 目前代码支持 | 尚缺生产等价/真实验证 |
|---|---|---|
| 核心 | Rust Axum、Tokio、SQLx、Postgres 原子任务创建、去重、租约与 fence | 全部老 API、历史 Hatchet 任务迁移 |
| 认证 | V1 兼容管理员 Cookie、密码、V2 token、限速 | 全部原版会话/审计和代理场景 |
| YouTube | yt-dlp 调度、频道/列表 CRUD、来源去重及定时扫描 | aria2 高并发续传、复杂代理与真实来源重试 |
| 字幕 | Whisper 兼容音频请求、分窗识别、指纹检查点、SRT/ASS、按编号翻译 | 所有原版 ASR、Index-Translate 和 RAG Agent 完整实现 |
| 视频 | 真正 FFmpeg H264/HEVC/AV1、VAAPI 回退、ASS 烧录 | 复杂原版 RenderSpec、长视频、多 GPU 性能 |
| 远程渲染 | V1 Windows Worker 协议，单次注册、独立凭证、领取、心跳、日志、Range、fenced 上传与完成 | 真实 Windows/Linux GPU 节点压测和断电恢复 |
| 配音 | YouDub 独立推理进程桥接，稳定任务 ID 和 V2 P2 归属约束 | 真正 VoxCPM2/Demucs 对齐和音质/时长验收 |
| 投稿 | Rust B 站 UPOS 分块上传、单次稿件创建、双 P 校验；可选原版投稿兼容层 | 真实账户测试、原生字幕/合集/话题/多平台对齐 |
| 资源 | 引用登记、延迟清理、故障/取消回收、播控保护、回收锁 | 海量并发、异常重启和 V1 历史目录清理 |
| 配置与 UI | 版本化设置、队列遥测、V2 独立 React 开发控制台 | 全部原版页面和 API 完全迁移 |

## 开发环境与安全

在 rust 目录运行 cargo fmt --check、cargo check --all-targets --locked、cargo test --all --locked、cargo clippy --all-targets --locked -- -D warnings。
在项目根目录运行 bash rust/scripts/smoke-isolated.sh，测试会创建一次性的回环 PostgreSQL Docker 容器，模拟 ASR 和投稿后端，通过 FFmpeg 真正编码合成视频，并联调完整 Remote Worker HTTP 协议。测试不会执行真正平台投稿。
V2 API 只监听指定回环端口，必须指定独立 DATABASE_URL、VIDEOROLL_V2_STORAGE_ROOT 和强内部 Token。不会在 API 启动时自动迁移或启动 Worker。
YouDub 模型必须使用独立 V2 运行目录，不能和原版共享。原生 B 站投稿和旧版投稿兼容适配器都需要专门的显式配置才会启用。
V2 React 页面只有在 VITE_RUST_V2_UI=true 时出现，生产界面默认没有变化。
原版 Windows Render Worker 通过单次使用的注册码加入，凭证与执行 token 都只属于 V2，不能访问其他作业的数据。

## 关键模块

- rust/src/api.rs：业务 API
- rust/src/scheduler.rs：工作流、租约、恢复、UNKNOWN 人工对账
- rust/src/storage.rs / assets.rs：对象、HTTP 流、延期回收
- rust/src/media：字幕识别、FFmpeg、本地与外部推理
- rust/src/remote_render：现有 Windows Render Worker v1 协议
- rust/src/bilibili.rs / publish.rs / publish_legacy.rs：平台投稿
- rust/src/sources.rs：频道/列表扫描
- rust/src/settings.rs / telemetry.rs：动态配置和队列统计
- rust/migrations：V2 表与权限约束
- rust/scripts/smoke-isolated.sh：隔离集成验收

## 完整替代前必须通过

1. 原版业务 API、设置、217 个路由和全部 React 页面及历史任务双向契约。
2. 所有 ASR Provider、Index-Translate 专用接口、RAG Agent/Embedding/知识库、YouDub 和字幕详细样式。
3. Windows Render Worker 实体 GPU、ffplayout 播控导入、长视频和中断恢复。
4. B 站多账号双 P、话题活动、合集、原生字幕、其他 SAU 投稿平台真实验收。
5. 多 Worker 并发、重启断点、无丢任务、无重复投稿、终态资产可靠回收。
6. 和旧版只读影子流量比对及完整回滚预案，合格后才能考虑切换。

本分支仍不满足全部生产等价条件。没有假称完成所有业务，也没有切换生产。
