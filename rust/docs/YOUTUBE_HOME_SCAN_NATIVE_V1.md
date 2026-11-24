# Rust YouTube 首页推荐流手动扫描 — 2026-11-24

原 Python POST /settings/youtube/home_scan/run 已映射到原生 Rust HTTP Handler。调用为同步手动执行，不能把接收到请求等同于已经创建任务。

本轮同时扩展了旧版 GET/PUT /settings/youtube：直接使用原 app_settings 中 youtube.settings 的 home_scan_enabled、home_scan_interval_minutes、home_scan_limit、home_scan_long_videos_only、home_scan_min_duration_seconds 字段，不另建影子配置表。旧版统计 home_scan_last_* 继续写同一行；home_scan_running 根据原 home_scan_lock_until 判断。Rust 更新设置时在同一事务中读取加锁的原行，防止丢失扫描锁。

核心实现：
- 读取 Rust 现有安全目录中的 Netscape cookies.txt；旧 Python Fernet 密文不私自猜测或解密，必须先迁移/导入到 Rust。HTTP Cookie 与 SAPISIDHASH 仅用于发送给 YouTube，不写入日志、数据库或响应。
- 直接使用 Rust 请求 YouTube sw.js_data 和 youtubei/v1/browse，动态读取公开 Web Client key/context，支持 continuation 翻页；解析常规 videoRenderer、lockupViewModel、shortsLockupViewModel 等视频卡片、时长、Shorts 与长视频筛选。无需 Python orchestrator HTTP 代理或 Python 爬虫进程。
- 复用原 Python 的 home_scan_lock_owner/home_scan_lock_until 行锁，Rust 维持心跳，失去租约则不继续创建任务；请求中断会停止心跳，待租约自然到期恢复。已经启动的工作流不会被误标为未创建。
- 调用现有 Rust Scheduler.create_youtube_source_task，继续使用 ingested_videos(platform,source_id) 唯一索引跨 Python/Rust 去重；同事务写入 tasks.created_by 和 origin_kind=youtube_home_scan 并创建真实 vr2_workflows/operation inbox，不伪造 Hatchet Job ID。
- 返回原 YouTubeHomeScanRunResponse 字段，包括发现数量、实际创建的任务、工作流 ID、去重、时长筛选统计、运行日志与采样 URL。
- YouTube 请求目标固定官方 https://www.youtube.com。测试网络改写仅在明确启用 VIDEOROLL_V2_ALLOW_TEST_YOUTUBE_HOME_LOOPBACK=true 且目标为 loopback HTTP 时开放。

验证：隔离 PostgreSQL、真正 Rust HTTP API 与本地 Innertube 模拟服务，覆盖动态 sw.js_data API key/clientVersion、Cookie 配置、原 Python 锁冲突、两页推荐、长视频/Shorts/未知时长、历史 ingested_videos 去重、重复执行、原任务来源归属和上游 503 后释放租约及统计错误。

未验证或未实现：自动周期调度 home_scan_enabled、真实登录 YouTube 的推荐流版本差异、IP/代理封锁与生产级整条媒体处理链。由于 UI/平台版本不断变化，接口仅计入部分映射，不能计为完整 Python 业务等价。未进行真实网页抓取和生产排队。
