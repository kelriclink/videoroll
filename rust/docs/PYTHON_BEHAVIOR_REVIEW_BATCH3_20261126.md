# Python 行为等价审计第三批

基线 2c208d4。并行复核八个方向，实施五组改动；原 Python 参考测试不等同于 Rust 端到端验收。

| 方向 | Python 证据 | 本轮 Rust 行为及边界 |
|---|---|---|
| RAG | rag.py:1061–1250,2735–3379 | 搜索/Wikipedia 按提供商、配置、查询和凭据摘要隔离的 128 条 TTL 缓存，32 个有界准入门，同查询合并、429 冷却、连续失败熔断、流式响应字节上限；仅单进程跨任务，不是跨进程 Redis 等价，页面正文缓存、自动重试及完整检索差分仍待。 |
| Groq VAD | processing.py:2459–2552,1940–1974 | 调用现有本地推理适配器的 CPU Silero，使用原阈值/最短语音/静音/边界配置；确认空语音才跳过，无法检测则继续上传；配置纳入 ASR 缓存身份。字幕逐段 VAD 时间轴精修、真实语音样本差分仍待。 |
| 投稿审核 | publish_review.py:20–145 | 恢复逗号/分号/换行词表、去 SRT 编号及时间行、ASS/HTML 标记、换行归一化，允许读取原存储键字幕；全文关键词检查及全文指纹比旧摘录检查更保守，AI 仍用有界摘录；既有审核指纹需重验。 |
| 播控 | playout_service.py:43–190,235–310 | 新链接优先保存的译名，180 UTF-8 字节限制、保留真实媒体扩展名、碰撞后缀、同一 Rust 链接重试保留原平面文件名，跨频道共享路径占用检查，修正 P2 回执视图排序字段。历史 Python 或旧嵌套路径仍保守拒绝重新分配，需专门恢复策略，未修改既有文件。 |
| 存储 | filesystem.py:86–102,142–205 | 新 Rust 上传统一专用 .vr2-upload.partial 临时名称，持有 inode 锁，过期 24h 后按小时扫限定 v2/artifacts/UUID 路径，单轮最多清 500 个；跳过活动锁、符号链接和其他文件，不清除 Python 旧临时文件或 yt-dlp 断点。目录遍历总量尚未做分页，需监测大目录性能。 |
| 合集 | bilibili_publisher/worker.py:195–386; bilibili_web_client.py:746–925 | 已核对原独立同步、96 次小时级重试与已有合集检查；仍待实现 Rust 侧确认后独立事件及真实安全隔离测试，不能与 archive POST 重试混用。 |
| 配音 | dubbing/youdub_native.py:153–289; dubbing/youdub_job.py | 已核对 GPU 等待、resident 取消、audio_only.flag、混音与对齐 SRT 交接；仍待完整 Rust 音频后置渲染及取消状态机。 |
| 标题翻译 | publish_meta_draft.py:48–105,185–241; index_translate.py:90–124 | 已核对模型选择、单行标题校验、带上下文失败后无上下文重试、已译标题优先；Rust 标题 AI 翻译及单平台自动草稿入口尚待。 |

验证：Rust cargo test --lib --locked 235/235；cargo check --bin videoroll-v2 --locked 通过；旧版 Python review+filesystem 14/14、Groq 23/23、playout 14/14。使用已有 Python 3.12 测试依赖路径，不安装包。实测安装的 CPU Silero FLAC 静音/纯音检测、缺失文件回退通过。未执行真实平台请求、Windows 硬件验收、最新 Release 或真实生产 schema 的新播控端到端测试。
