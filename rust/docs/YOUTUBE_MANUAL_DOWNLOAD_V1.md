# Rust 旧版 YouTube 手动下载：安全的阶段性兼容

2026-11-24：新增 `POST /tasks/{task_id}/actions/youtube_download`。原 Python 接口直接同步下载并返回 `YouTubeDownloadActionResponse`（metadata、metadata_asset、video_asset、cover_asset）。Rust 的下载工作由持久化调度器和独立 Worker 执行，不能把仅仅排队伪装为下载完成。

- **已完成的视频**：仅针对当前 Rust 托管的 YouTube 任务、有效 YouTube URL、磁盘中确有非空 V2 视频资源，才读取/补建真实 metadata 资源，按原 `AssetRead` 字段返回完整响应；封面允许为 `null`。缺失或损坏的元数据不伪造。
- **尚未完成**：复用 `/tasks/{task_id}/actions/youtube_download_enqueue` 所使用的 Rust 持久化调度器，任务只入队一次。因旧版同步 DTO 尚不可用，明确返回原风格的 HTTP 409 `YOUTUBE_DOWNLOAD_IN_PROGRESS`，提示通过 `youtube_download_progress` 轮询后再请求。**这不是旧版同步调用的完全等价实现**，旧前端仍需适配异步反馈。
- **旧 Python/Hatchet 保护**：检查原表 `operation_inbox` 中对应 `youtube-download:{task_id}` 的 `pending/processing/unknown` 记录；只要可能还归旧服务所有，就拒绝 Rust 接管，避免两个下载器操作同一文件。
- **结果恢复**：已记录 `succeeded` 但资源丢失时返回 `YOUTUBE_DOWNLOAD_RESULT_RECOVERING`，不得重新下载、不得宣称完成；`unknown` 同样必须人工核对。
- **安全边界**：不支持非 YouTube/非 Rust 托管任务，不直接在 HTTP 线程运行 yt-dlp。文件路径通过现有存储沙箱校验，原 Asset DTO 排除内部 `task_id` 字段，不向外暴露服务器本地绝对路径。

验证：隔离 PostgreSQL、真实 HTTP 入队幂等、原 `YouTubeDownloadActionResponse` Pydantic 结构检查、失效元数据拒绝、原 Python 活跃租约防冲突、成功回执缺资产不重试。仍未使用真实 YouTube 下载，也未完成生产同步契约等价签收。
