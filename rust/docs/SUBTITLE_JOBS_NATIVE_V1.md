# Rust 原生旧版字幕创建接口：阶段性兼容

2026-11-24：实现 `POST /subtitle/jobs`。对 `runtime_owner=rust` 的任务，复用 Rust 持久化调度器创建真实字幕阶段，返回旧版 JSON `job_id/status`。原 `GET /subtitle/jobs/{job_id}` 已能读取 Rust 阶段的 ID。**不插入无人消费的旧 `subtitle_jobs(status=queued)` 记录，不派发 Python/Hatchet**。它是业务功能适配，而非 Python 内部所有状态字段的全面等价。

## 并发与安全

- 输入必须是 `storage` 类型、当前任务最新的 `video_raw` V2 资源键；拒绝资源不匹配、没有原始视频、已停止或已投稿任务。
- 先复用原 `subtitle_jobs` 表内的活跃 Python 任务；Rust 原生阶段继续使用 `Scheduler::restart_at` 的任务锁、代际和配置匹配确保幂等及冲突检测。
- 原生字幕阶段完成前即使已产生 `subtitle_srt`，也仍能复用活跃作业。没有活跃作业而已有 SRT 时拒绝伪造新的 ASR 任务。
- 旧 `asr`、`translate`、`output.render` 参数转换为真实 Rust 字幕配置，未移植的 YouTube 字幕源选择、特定 ASR 引擎、显式词库、指定字幕重翻译、输出前缀及 Python `after_render` 回调必须显式拒绝，不能静默丢弃。
- 未显式传入的原 V1 手动字幕参数使用原 Python 默认值，尤其是 `translate.enabled=false` 与 `render.burn_in=false`，不会误用自动处理配置而擅自翻译或渲染硬字幕。
- 旧请求默认 `prefer_youtube_subtitles=true`，目前无法等价自动使用 YouTube 字幕，调用方必须设置 `prefer_youtube_subtitles=false` 和 `youtube_subtitle_mode=off` 才能使用原生 ASR 子集。

## 验收边界

通过 Rust 参数测试及临时 PostgreSQL 上的 HTTP 创建、持久化 operation_inbox 阶段、重复请求幂等、不同参数冲突、SRT 已写入但作业仍活跃时的重复操作、原 Python 活跃作业复用，以及后续 GET 状态读取测试。未执行生产任务及真实平台操作。

**仍需完成**：原 Python `SubtitleJobCreate` 所有功能、原 `subtitle_jobs` 内部状态写入契约、工作流恢复全链路及真实模型生产等价验收。矩阵只标为部分原生适配。
