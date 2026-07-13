# VideoRoll（合规版）项目规格：模块化视频处理流水线

> 目标：对**你拥有版权/已获授权/允许再分发**的视频，实现“内容接入 →（可选下载）→ 语音识别/翻译 → 生成字幕 →（可选）压制/封装 →（可选）投稿到哔哩哔哩”，并通过网页进行任务管理、人工审核与自定义投稿信息。
>
> 非目标：自动抓取/搬运他人热门内容、绕过平台规则、批量转载、规避风控。

---

## 0. 2026-07 当前实现基线

本文档保留产品能力、产物契约和任务模型。当前运行拓扑已从早期“同进程模块组装”升级为独立 Compose 进程：Orchestrator、字幕 API/worker、YouTube 接入、Bilibili 投稿 API/worker、社交发布 API/worker/scheduler、outbox dispatcher 和 egress gateway 分别运行。

- 浏览器只经 Web 与 Orchestrator 访问业务能力；内部服务不公开宿主机端口。
- 异步副作用使用 outbox/inbox、操作键、lease 和 heartbeat 恢复；发布的未知状态必须人工对账。
- RAG/网页抓取只可经 egress gateway 出网；内部调用需要服务身份 token。
- schema 使用 Alembic 迁移；交互式浏览器使用短期 desktop grant。

实现细节以[架构指南](ARCHITECTURE.md)和[部署指南](DEPLOYMENT.md)为准。本文件中标记为“建议”或“方式 A/B”的内容是产品演进设计，不表示生产进程仍按旧单体模式运行。

---

## 1. 当前显著能力

当前实现已经从“普通字幕翻译流水线”升级为带可观测 RAG Agent 的视频翻译系统。重点不是把所有词都查一遍，而是在翻译前判断“哪些词确实需要外部知识”，再把可信知识注入翻译 prompt。

- **字幕翻译 RAG gate**：每个翻译 block 开始前，LLM 先结合当前字幕和前文 summary 判断哪些术语真的需要检索，避免把基础词、局部变量、普通短语都塞进知识库。
- **PostgreSQL + pgvector 知识库**：保存术语、译法、领域、别名、解释、来源、置信度、状态和 embedding，翻译时按向量相似度召回并注入上下文。
- **主 Agent + 子 Agent 架构**：每个字幕 block 创建一个 `rag_master` 主 Agent；主 Agent 负责术语 gate、调度并发子 Agent、汇总结果；每个术语研究子 Agent 维护自己的 observation/evidence 上下文。
- **工具调用循环**：子 Agent 可按需调用 `rag_lookup`、`wiki_search`、`search_web`、`fetch_url`、`finish`。工具选择由 LLM 根据当前 observation 决定，程序只做超时、重试、fallback 和写库安全控制。
- **轻量 Agent Runtime**：借鉴 LangGraph/OpenAI Agents SDK/PydanticAI 一类框架的核心做法，但不引入重依赖；内部统一 tool schema、结构化输出校验、预算控制、状态迁移、错误分类和 trace 事件。
- **Agent Skills**：支持从 `src/videoroll/apps/subtitle_service/skills/` 和 `data/agent_skills/` 加载 skill 能力包。当前内置通用术语研究、来源验证/写库、Wikipedia 百科研究和 SearXNG 网页研究。Skill 通过 `instructions/resources/allowed_tools` 影响子 Agent 的执行策略，但具体动作仍通过已注册 tool 完成，避免任意脚本执行。格式见 `docs/AGENT_SKILLS.md`。
- **Wikipedia Tool**：固定使用 English Wikipedia API，适合百科型专有名词和通用背景知识；不用在配置里维护各种不通用的 wiki 地址。
- **SearXNG Tool**：可接入自建 SearXNG，配置填写 base URL，系统自动请求 `/search?q=...&format=json` 并过滤搜索引擎自身页面等无效结果。
- **SearXNG 参数配置**：翻译设置支持配置 `categories`、`engines`、`fallback_engines`、`language`、`safesearch`、`time_range` 和 `pageno`。不指定 `engines` 时使用实例默认引擎，默认搜索为空时使用 fallback 引擎再次尝试。
- **网页正文读取 Tool**：当搜索摘要不足时，Agent 可以打开候选链接抽取正文，再交给 LLM 总结证据。
- **Verifier 入库保护**：候选术语会经过 verifier 判断来源是否支撑、是否和字幕上下文一致、置信度是否足够；不满足条件时不会污染长期知识库。
- **并发术语研究**：翻译设置里可调整 RAG Agent 并发数和超时时间，让多个需要查证的术语同时研究；子 Agent 结束后返回结构化 `AgentResearchResult`，由主 Agent 合并回当前翻译上下文。
- **独立 embedding 配置**：翻译模型和 embedding 模型的 API key/base URL 分开；embedding 支持 OpenAI 兼容接口和本地模型目录。
- **独立大模型重试配置**：翻译大模型请求的失败重试次数可在翻译设置中单独配置，不和 embedding 请求混用。
- **Web 管理与可观测性**：知识库页面支持查看、新增、删除条目；Dashboard 可查看资源监控、主 Agent/子 Agent 运行树、历史 trace 和每步 JSON 输入输出。

核心翻译流程：

```text
字幕 block + 前文 summary
  -> 创建 rag_master 主 Agent
  -> RAG gate 判断需要检索的术语
  -> 先查询 pgvector 知识库
  -> 未命中时并发启动术语研究子 Agent
  -> 子 Agent 按 observation 决定 rag/wiki/search/fetch/finish
  -> LLM summarize 生成候选译法
  -> verifier 校验来源和上下文
  -> 写入知识库或标记为 pending/context_only/skipped
  -> 子 Agent 返回 hit/context_card
  -> 主 Agent 汇总 RagContext
  -> 将精简后的术语卡片和知识卡片注入翻译 prompt
  -> 输出当前 block 翻译并更新 summary
```

子 Agent 的 trace 用于调试和审计，不直接塞回翻译 prompt。最终进入翻译 prompt 的是主 Agent 汇总后的结构化上下文：

- `term_cards`：术语、译法、领域、说明、来源、置信度。
- `knowledge_cards`：文档型背景知识。
- `hits`：原始知识库命中，用于记录匹配和使用次数。

Agent trace 是规范化事件流，每步包含 `event_id`、`span_id`、`kind`、`action`、`status`、`duration_ms`、`error_type`、工具 schema、预算计数和输入输出摘要。Dashboard 以这些字段展示主 Agent/子 Agent 树和每步 JSON。

---

## 2. 总体拆分：三大模块 + 一个编排层（Orchestrator）

### 2.1 模块列表与边界

1) **subtitle-service（字幕/翻译模块）**
- 只负责：从视频/音频生成字幕（转写 + 可选翻译 + 可选润色/术语表 + 排版对齐），并按配置输出字幕文件；可选产生“带字幕的视频”（硬字幕 burn-in / 软字幕 soft-sub）。
- 不负责：内容来源发现、YouTube 扫描下载、B 站投稿、账号凭据管理。

2) **youtube-ingest（YouTube 抓取/接入模块）**
- 只负责：从“白名单/已授权源”发现视频（扫描频道/播放列表/手动 URL），拉取**元信息**、做许可/白名单校验、创建任务；可选“下载到存储”（可插拔）。
- 不负责：ASR/翻译/压制、B 站投稿。

3) **bilibili-publisher（B 站上传/投稿模块）**
- 只负责：接收一个“最终发布包”（final video + cover + meta），完成上传与投稿（以及状态回写）。
- 不负责：视频怎么来的、字幕怎么做的、YouTube 如何扫描。

4) **orchestrator（编排层 + Web 管控台 + 队列）**
- 负责：任务状态机、队列分发、产物索引（assets）、人工审核、模板管理、凭据加密存储、审计日志与告警。
- 原则：不写平台/ASR 的细节逻辑，只调度模块并记录结果。

### 2.2 两种落地方式（推荐先 1 后 2）

**方式 A：Monorepo + 进程级解耦（推荐起步）**
- 同仓库放四个服务/包，但每个模块都有自己的入口、配置、依赖、接口。
- 好处：开发快、改动集中；上线时也能拆分部署。

**方式 B：真正微服务（HTTP/RPC + 独立部署）**
- 各模块完全独立仓库或独立镜像，由 orchestrator 通过 HTTP 调用。
- 好处：边界最清晰；坏处：前期维护成本高。

---

## 3. 统一“产物契约”（Artifact Contract）：模块解耦关键

模块之间只通过两类东西交互：
1) **DB 里 task 的状态与元信息**
2) **对象存储里的产物 key（S3/MinIO）**

### 3.1 存储 Key 规范（示例）

建议按 `tenant/account/task_id` 分层，便于多账号与隔离：

- `raw/{task_id}/video_{sha256[:16]}.mp4`（原视频，建议内容寻址避免覆盖）
- `raw/{task_id}/metadata_{sha256[:16]}.json`
- `work/{task_id}/audio_{sha256[:16]}.wav`
- `sub/{task_id}/segments_{sha256[:16]}.json`（ASR 分段结构化结果）
- `sub/{task_id}/subtitle_src_{sha256[:16]}.srt`
- `sub/{task_id}/subtitle_zh_{sha256[:16]}.srt`
- `sub/{task_id}/subtitle_zh_{sha256[:16]}.ass`
- `final/{task_id}/video_burnin_{sha256[:16]}.mp4`
- `final/{task_id}/video_softsub_{sha256[:16]}.mkv`
- `final/{task_id}/cover_{sha256[:16]}.jpg`
- `meta/{task_id}/publish_meta.json`
- `meta/{task_id}/publish_result.json`
- `logs/{task_id}/stage_{name}.log`

### 3.2 产物对象最小字段（建议）

每个产物在 DB 的 `assets` 表里记录：
- `task_id`
- `kind`（枚举：video_raw/audio_wav/subtitle_srt/subtitle_ass/video_final/cover/log 等）
- `storage_key`
- `sha256`（可选但强烈建议）
- `size_bytes`
- `duration_ms`（视频/音频）
- `created_at`

---

## 4. 任务状态机（由 Orchestrator 维护）

建议把任务拆成“内容接入/下载”、“字幕制作”、“发布”三段，每段内部再细分 stage；每个 stage 都是**幂等**、可重试、可断点续跑。

### 4.1 建议状态枚举（可按需要裁剪）

- `CREATED`（任务创建）
- `INGESTED`（已拉取元信息/已入库）
- `DOWNLOADED`（原视频已入存储）
- `AUDIO_EXTRACTED`
- `ASR_DONE`
- `TRANSLATED`（如需翻译）
- `SUBTITLE_READY`（字幕文件产出）
- `RENDERED`（burn-in 或 soft-sub 产出；可选）
- `READY_FOR_REVIEW`（进入人工审核）
- `APPROVED`（审核通过）
- `PUBLISHING`
- `PUBLISHED`
- `FAILED`（带 error_code，可重试标记）
- `CANCELED`

### 4.2 幂等/重试原则

- 每个 stage 以 `task_id + stage_name` 作为幂等键；产物存在则跳过或做一致性校验。
- `FAILED` 不代表终止，保存 `error_code` 和 `is_retryable`，支持手动/自动重试。
- 发布阶段需要额外幂等：若已拿到 `aid/bvid`，重复提交应改为轮询/对账，而非再次投稿。

---

## 5. subtitle-service（字幕/翻译模块）设计（重点）

### 5.1 支持的业务场景

1) **仅生成字幕文件**
- 输入：你的本地视频（上传到存储）、或系统已有 `raw/{task_id}/video.mp4`
- 输出：`subtitle_zh.srt`（必选）+ 可选 `subtitle_zh.ass / vtt / segments.json`

2) **生成字幕并压制硬字幕（burn-in）**
- 输出：`final/{task_id}/video_burnin.mp4`
- 适合：B 站播放体验、无需依赖字幕轨道支持

3) **生成字幕并封装软字幕（soft-sub）**
- 输出：`final/{task_id}/video_softsub.mkv`（更推荐 MKV 容器）
- 适合：本地播放/归档；投稿平台是否展示字幕轨道要看平台能力

4) **双语字幕**
- 输出：`subtitle_bi.srt` 或 `subtitle_bi.ass`（上/下行）

### 5.2 内部流水线（可拆分 job）

**S1 音频提取（FFmpeg）**
- 输入：视频文件
- 输出：`audio.wav`（建议单声道 16k）

**S2 ASR（语音识别）**
- 引擎可插拔（本地 ASR / faster-whisper / 其他）
- 输出：`segments.json` + `subtitle_src.srt`

**S3 翻译（可选）**
- 输入：`segments.json` 或 `subtitle_src.srt`
- 输出：`subtitle_zh.srt`
- 可插拔 provider：LLM / 翻译引擎。
- 翻译前可启用 RAG gate：结合当前 block 和前文 summary 发现需要查询的术语；主 Agent 先查 PostgreSQL/pgvector，未命中时并发启动术语研究子 Agent。
- 子 Agent 通过工具调用循环自行决定下一步：本地 RAG、Wikipedia、SearXNG、网页正文读取或结束研究。
- RAG 结果不会无条件写入长期知识库；必须经过 verifier 校验来源、上下文一致性和置信度。
- 支持手动知识库条目、自动学习条目、context_only 临时提示和 pending 待确认条目。

**S4 字幕排版与对齐（强烈建议做）**
目标：可读性与节奏一致
- 断句：按标点/停顿/最大行宽
- 约束：每条字幕最短/最长时长、CPS（每秒字符数）上限
- 输出：`subtitle_zh_fixed.srt` + 可选生成 `ASS`（样式）

**S5 产物输出**
- 仅字幕：写入 `sub/`
- burn-in：FFmpeg + ASS（推荐用 ASS 样式作为输入，效果更好）
- soft-sub：容器封装（建议 MKV，MP4 的字幕支持更有限）

### 5.3 对外接口（建议：HTTP + Job 模式）

#### 5.3.1 创建字幕任务
`POST /subtitle/jobs`

请求（示例）：
```json
{
  "task_id": "uuid",
  "input": { "type": "s3", "key": "raw/{task_id}/video.mp4" },
  "asr": { "engine": "faster-whisper", "language": "auto" },
  "translate": {
    "enabled": true,
    "target_lang": "zh",
    "provider": "llm",
    "glossary_id": "glossary_v1",
    "bilingual": false
  },
  "output": {
    "formats": ["srt", "ass", "json"],
    "render": { "burn_in": false, "soft_sub": false, "ass_style": "clean_white" }
  },
  "output_prefix": "sub/{task_id}/"
}
```

返回（示例）：
```json
{ "job_id": "uuid", "status": "queued" }
```

#### 5.3.2 查询任务状态
`GET /subtitle/jobs/{job_id}`

返回（示例）：
```json
{
  "job_id": "uuid",
  "status": "running",
  "progress": 0.42,
  "artifacts": [
    { "kind": "subtitle_srt", "key": "sub/{task_id}/subtitle_zh.srt" }
  ],
  "logs_key": "logs/{task_id}/stage_subtitle.log"
}
```

### 5.4 “只出字幕 vs 压制字幕”怎么落地（推荐的参数化）

在同一条 API 里用 `output.render` 控制即可：
- 只出字幕：`burn_in=false, soft_sub=false`
- 硬字幕：`burn_in=true`
- 软字幕：`soft_sub=true`
- 两者都要：都设为 `true`

> 产物 key 不要复用同名，避免幂等冲突；建议 burn-in/soft-sub 输出到 `final/`。

---

## 6. youtube-ingest（YouTube 接入/抓取模块）设计（与发布解耦）

> 合规核心：**只允许白名单源**（你自己的频道/你已获授权的频道或播放列表/CC 许可明确的源），并把许可信息写入任务，支持人工补证据。

### 6.1 能力清单

1) **Source 管理（白名单）**
- `channel_id` 白名单（own/authorized）
- `playlist_id` 白名单（authorized/cc）
- 单条 URL（手动入队，必须选择 license 并可上传 proof）

2) **扫描发现（Poller）**
- 每 N 分钟扫描白名单源，获取新视频 `source_id`
- 去重：`platform + source_id` 唯一
- 写入 orchestrator：创建 `task`（状态 `INGESTED`）

3) **（可选）下载**
- 可以把“下载器”做成可插拔策略：
  - `manual_only`：只写元信息，不自动下载（最稳、最不触碰平台条款）
  - `downloader_plugin`：仅对 `license=own/authorized` 开启；写清楚合规责任

### 6.2 对外接口（示例）

#### 6.2.1 手动入队单条视频
`POST /youtube/ingest`
```json
{
  "url": "https://www.youtube.com/watch?v=xxxx",
  "license": "own",
  "proof_url": null,
  "download": false
}
```

#### 6.2.2 扫描频道/播放列表
`POST /youtube/scan`
```json
{
  "source_type": "channel",
  "source_id": "UCxxxx",
  "since": "2026-02-01T00:00:00Z",
  "download": false
}
```

返回建议包含：
- `discovered_count`
- `created_task_ids`
- `skipped_duplicates`
- `skipped_not_whitelisted`

### 6.3 与 subtitle-service 解耦的点
- youtube-ingest 只产生 `raw/metadata.json`（以及可选 `raw/video.mp4`）
- subtitle-service 不关心 YouTube，只关心 `raw/video.mp4` 是否存在

---

## 7. bilibili-publisher（B 站上传投稿模块）设计（只接 final 包）

### 7.1 输入契约：Publish Package

`POST /bilibili/publish`
```json
{
  "task_id": "uuid",
  "account_id": "bili_acc_1",
  "video": { "type": "s3", "key": "final/{task_id}/video_burnin.mp4" },
  "cover": { "type": "s3", "key": "final/{task_id}/cover.jpg" },
  "meta": {
    "title": "标题",
    "desc": "简介（需含授权/来源说明）",
    "tags": ["tag1", "tag2"],
    "typeid": 17,
    "copyright": 1,
    "source": "",
    "dtime": null
  }
}
```

### 7.2 输出：发布结果

```json
{
  "state": "submitted",
  "aid": "123",
  "bvid": "BVxxxx",
  "response": { "raw": "..." }
}
```

### 7.3 关键工程点（不涉及绕过风控）
- 分片/断点续传：上传过程要可恢复（网络抖动常见）
- 速率限制：同账号投稿间隔、每日上限（避免触发平台限制）
- 凭据安全：cookie/csrf 不在日志里打印，DB 加密存储
- 幂等：同 task 重试 publish 不应重复投稿；优先对账/轮询状态

---

## 8. Orchestrator（编排层）要做的“产品能力”

### 8.1 Web 管控台核心功能
- 任务列表：状态筛选、搜索、批量重试/取消
- 任务详情：查看每 stage 的日志与产物；字幕文件在线预览/编辑；投稿 meta 编辑；审核通过/驳回
- Dashboard：任务状态、队列概览、CPU/内存/Intel GPU 资源监控、RAG 主 Agent/子 Agent 运行树和历史 trace
- 知识库管理：查看、新增、删除 RAG 条目；触发 embedding 重建
- 模板中心：标题/简介/标签/分区/动态文案模板，支持变量（如 `{source_title}`、`{publish_date}`）
- 账号与凭据：B 站凭据录入与轮换提醒；权限控制
- 系统设置：并发数、重试策略、队列开关、通知（Webhook/邮件等）

### 8.2 队列编排建议
如果用队列（推荐）：
- `queue:ingest`（youtube-ingest 相关）
- `queue:subtitle`（subtitle-service 相关）
- `queue:render`（ffmpeg 压制）
- `queue:publish`（bilibili-publisher）

每个队列可独立扩容 worker，且失败不影响其他队列。

---

## 9. 最小可运行里程碑（建议）

**M1：本地视频 → 中文字幕 SRT**
- Web 创建任务 + 上传本地视频到存储
- worker 执行：提取音频 → ASR → 产出 `subtitle_zh.srt`

**M2：字幕压制**
- ASS 样式模板
- burn-in 输出 `video_burnin.mp4`

**M3：B 站投稿（先做 mock，再做真上传）**
- 先让 bilibili-publisher 返回 mock 结果，把编排、审核流跑通
- 再逐步实现封面上传/视频上传/提交稿件

**M4：YouTube 授权源扫描**
- 只做白名单源发现 + 入队（download 先关）

**M5：RAG Agent 翻译增强**
- PostgreSQL + pgvector 知识库
- 主 Agent gate + 术语噪声过滤
- 子 Agent 工具循环：RAG / Wikipedia / SearXNG / fetch URL
- verifier 保守入库
- Dashboard trace 与知识库管理页面

---

## 10. 你需要提前确定的 5 个关键选项（决定代码形态）

1) 技术栈：**Python（FastAPI + Celery）** 还是 **Node（NestJS + BullMQ）**
2) ASR：本地（faster-whisper）还是云（可插拔）
3) 翻译：是否需要、是否需要术语表、是否需要双语
4) 字幕输出：只字幕 / burn-in / soft-sub / 全要
5) 发布策略：全自动还是必须人工审核后才能投稿
