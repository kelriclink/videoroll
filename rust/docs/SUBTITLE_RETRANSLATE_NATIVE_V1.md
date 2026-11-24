# Rust 原生指定字幕重新翻译：阶段性兼容验收

2026-11-24：原生迁移 `POST /tasks/{task_id}/actions/subtitle_retranslate`，沿用原 Python API 的 1-based `indices` 和 `job_id/status` DTO。当前不是空路由或无消费的 `subtitle_jobs` 假排队；请求使用 Rust 原持久化 `Scheduler::restart_at(Stage::Subtitle)` 创建真实工作流和独立代际，后续由 Rust Worker 处理。

## 执行与资产语义

- 仅允许当前由 Rust 托管的、未停止/未投稿的任务。要求已确认成功的 Rust 字幕任务、原生 `v2/sub/{task_id}/{job_id}/segments.json` 对齐快照及对应 SRT。旧 Python 格式、没有分段快照或失败的字幕工作不允许冒充成功。
- 从原 `assets`、原 `tasks` 和 Rust 已有工作流读取所有基础信息，保留原 `app_settings` 中模型配置；不新增 PostgreSQL 业务表或影子业务表。
- 读取且钉住原始快照内容 SHA256、来源字幕作业、已翻译分段与时间轴。最多 5000 个原始分段，单批最多重新翻译 500 个不同的序号，单份快照最多 8 MiB；默认拒绝超出上限，不截断待翻译范围。
- 继承原工作流 `subtitle` 翻译配置（必要时补当前默认值），仅接受真实 OpenAI 兼容 Provider 或 Index-Translate。调用真实配置的模型网络接口并记录用量，使用 1:1 分段映射，拒绝丢失、重复或空翻译。没有 `mock`、`noop` 等隐藏降级。
- 将选中分段的文本更新到克隆后的翻译快照中，严格保留未选分段、分段时间戳与双语副文本；写入新的 `segments.json`、SRT、ASS，并按原 `assets` / `subtitles` / `storage_objects` / `storage_object_refs` 业务路径登记资产与版本。原文件不被覆写。
- 字幕阶段完成后使用现有 Render 阶段生成新视频；针对选择性重翻译明确跳过旧 `video_final` 缓存，避免沿用包含旧字幕的已渲染视频。`auto_publish=false`，不因重新翻译而自动再投稿。
- 输入校验拒绝不存在分段、时间戳不一致、无快照、非原生资源或正在运行的旧 Python 字幕任务。重复请求在同一代际和参数下返回同一个作业，不同参数冲突时拒绝覆盖。
- 执行时重新核对 SHA256，保存带来源与计划的结果快照作为重启 checkpoint；新一轮模型请求及每次资产写入均由当前租约守卫，过期 Worker 不会被允许继续接入资产引用。

## 已有测试证据

- Rust 单元测试：序号归一化、空序号与越界拒绝、源译文时间轴不一致拒绝、仅选中分段替换与原始时间轴保持。
- 临时 PostgreSQL/本地模型/实际 Rust API + Worker：OpenAI 兼容模型选中第 1、3 句、原第 2 句字节文本保留；新增 SRT、ASS、`video_final`，原文件不变，整条工作流成功且没有平台投稿。
- 第二条独立隔离任务：Index-Translate 模拟编号翻译 API 只重新翻译第 2 句，其余两句保持旧文案。
- 重复点击、异构请求冲突、无效序号无新任务、原服务不启动、隔离模型不含生产密钥均有检查。运行环境为 disposable Postgres + 127.0.0.1 模拟模型。

## 尚未实现/签收

- Python 历史 `translated_segments_key` 的所有旧版序列化形式、原 Translation Agent 的上下文记忆和逐句研究增强、完整旧 `subtitle_jobs` 内部日志/状态写入、生产长视频及真实模型延迟/断点故障注入尚未完整等价。
- Python 接口原允许最多 5000 个请求条目；Rust 原生目前每次最多 500 个不同分段，超过会显式返回错误。
- 当前是原生 V1 **部分适配**，并非生产等价已签收。Rust 尚未投入生产，测试不操作 Python/Hatchet 或真实账号。
