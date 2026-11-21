# Rust 重构设计决策与遗留契约（2026-11-21）

## 代码基准

- 旧系统 Commit：`70c4d1a62ebee31815094bd152bec3aff844dff9`
- 旧系统根目录：`src/videoroll/`（第一方 Python 201 个，约 65,405 行）
- React 入口：`src/web/`（约 20,627 行）
- 播控 ffplayout、原生 Windows Render Worker、YouDub/SAU 均是外部组件或独立工作进程
- Rust 工作分支：`rewrite/rust-v2`，专用 worktree，绝不直接替换 current 软链接

## 数据/兼容边界

`Task.status` 是面向用户的业务状态，原版 `PipelineRun` 是 Hatchet 关联状态，`SubtitleJob` / `RenderExecution` / `DubbingJob` / `PublishJob` 有自己的租约、完成条件与历史。新版本必须保留审计级事件记录，不能把它们强行压缩成一个枚举。

原版任务源兼容旧 `created_by` 文本标记与新 `origin_kind`、`restart_generation`。自动配置在**各个阶段开始时**读取，不是创建任务时冻结全部开关。原版已支持 YouTube 原生字幕优先、Groq Whisper、External Whisper、OpenVINO、本地 faster-whisper、RAG 术语/知识库、选择性翻译、SRT/ASS、GPU 渲染、YouDub 音频和独立 P2、Bilibili 双 P 原子提交、硬字幕后原生字幕同步。新版本要逐项测试这些能力而不是删除。

## 投稿不可逆边界

Bilibili `add_archive` 不是强幂等外部提交；网络断开可能已提交成功。V2 将任务的“请求已发出”和“平台完成回执”分开持久化。未知状态不允许自动重发；人工核实才可以解除阻断。原版 `PublishBatch` 基于多个目标的状态合并，成功+失败是 `partial_failed`，不会全局判定成功。保留该语义。

## 资源删除边界

原版 `storage/filesystem.py` 使用共享文件根目录；其他模块还有 workdir、终态资产、发布延迟、Render 缓存等清理机制。V2 采用明确的 `vr2_objects` / `vr2_object_refs`，并通过数据库触发器阻止 GC 正在删除时新增引用。任何已经被 v1 `assets` / `subtitles` 引用的文件，以及非 `v2/` 路径，都不允许由 v2 GC 清理。

## 安全边界

无外部公网监听、无启动即自动迁移、无生产 DB 写入、无真实账户投稿。用户密码使用原版 PBKDF2 SHA256 格式，签名 Cookie 兼容原版算法；内部 Worker 需要独立强 Token。URL 采集仅限合法 YouTube 视频 ID，不允许任意 URL SSRF 下载。媒体上传使用显式 MIME/后缀白名单，统一校验相对存储键与符号链接。

## 具体需验证的未知项

- V2 Worker Gateway 的生产认证协议和旧 Worker SDK 迁移边界。
- 严格与旧版一致的 SRT/ASS 换行、双语字号、ASR 切段与 GPU 参数。
- 所有自动模式的“运行时读取设置”与模型 Provider 继承语义。
- 旧版运行中的历史 PipelineRun、Upload Outbox 和未知投稿的迁移。
- ffplayout 媒体硬链接/复制与原视频资源删除之间的引用隔离。
