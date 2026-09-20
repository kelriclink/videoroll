# VideoRoll 当前项目规格

基线：2026-09-20。

本文描述**当前已经实现的产品和领域模型**，不是未来设计提案。

## 1. 产品目标

VideoRoll 将一个已授权视频从“素材”推进到“可发布成品”，并提供全过程追踪：

```text
接入
→ 下载/上传
→ ASR
→ 翻译/RAG
→ 字幕
→ 渲染
→ 审核
→ 投稿
→ 播控/归档
```

系统同时支持人工控制和自动流水线。

## 2. 合规边界

Source License：

- `own`；
- `authorized`；
- `cc`；
- `unknown`。

Task 保存授权类型和可选 proof URL。

VideoRoll 提供记录与流程约束，但不会替部署者判断真实授权是否合法。

## 3. 输入来源

当前 `SourceType`：

- `youtube`；
- `local`；
- `url`。

主要入口：

- 新建 Task；
- 本地视频/封面上传；
- YouTube 单条 URL；
- YouTube Source 扫描；
- Remote API。

## 4. Task

Task 是主聚合根。

关键字段：

- source type/url/license/proof；
- status；
- stopped status；
- priority；
- queue position；
- retry/error；
- lease/lock；
- publish batch compatibility pointer。

## 5. Task 状态机

```text
CREATED
INGESTED
DOWNLOADED
AUDIO_EXTRACTED
ASR_DONE
TRANSLATED
SUBTITLE_READY
RENDERED
READY_FOR_REVIEW
APPROVED
PUBLISHING
PUBLISHED
FAILED
CANCELED
```

不是所有任务必须经过每个状态。例如关闭翻译、只生成字幕或人工控制时可以跳过部分业务阶段。

## 6. Asset

`AssetKind`：

- `video_raw`；
- `metadata_json`；
- `audio_wav`；
- `segments_json`；
- `subtitle_srt`；
- `subtitle_ass`；
- `video_final`；
- `cover_image`；
- `log`；
- `publish_result`。

Asset 是数据库 metadata + shared filesystem object 的组合。

## 7. YouTube

### 单条接入

支持：

- metadata；
- download；
- auto pipeline；
- source ID 去重。

### Source

类型：

- channel；
- playlist。

配置：

- enabled；
- scan interval；
- scan limit；
- auto process；
- license/proof。

扫描保存开始/结束、发现数量、创建数量、自动启动数量、重复跳过、错误和 scan lock。

### 首页推荐扫描

Settings 支持：

- enabled；
- interval；
- limit；
- long-video filter；
- minimum duration；
- manual run。

## 8. Auto Profile

自动模式覆盖：

- SRT / ASS；
- burn-in；
- soft subtitle；
- ASS style；
- video codec；
- Intel GPU；
- quality / preset；
- primary/secondary font scale；
- ASR engine/language/model；
- YouTube subtitle mode；
- translation；
- bilingual；
- target language；
- provider/style/summary；
- auto publish；
- publish platforms；
- Bilibili category/title/cover/reprint strategy。

## 9. ASR

引擎：

- faster-whisper；
- OpenVINO；
- External Whisper-compatible；
- Groq；
- Cloudflare Workers AI。

本地模型管理：

- list；
- download；
- upload；
- delete；
- model download proxy test。

运行诊断：

```text
GET /subtitle/hardware/intel
```

返回 DRM 与 OpenVINO device visibility。

## 10. 翻译

Provider 路径包括：

- mock / noop；
- OpenAI-compatible；
- Cerebras-compatible API type。

功能：

- batch；
- dynamic summary；
- configurable retries；
- thinking stream/trace；
- checkpoint resume；
- bilingual；
- RAG context；
- Translation Memory；
- validator；
- targeted repair。

## 11. RAG

设置覆盖：

- top_k / min_score；
- embedding；
- dictionary；
- auto term discovery；
- auto learn；
- SearXNG；
- Wikipedia；
- domain；
- Agent parallelism；
- timeout；
- external request budget；
- token budget；
- cost budget；
- Agent Skills。

## 12. Agent Runtime

持久化：

- `translation_agent_runs`；
- `translation_agent_events`；
- checkpoint；
- checkpoint version/time；
- lease owner/until；
- parent run；
- result/error。

Runtime 负责：

- tool registry；
- input/output schema；
- budget；
- timeout；
- cancel；
- rate limit；
- trace；
- nested runtime。

UI 可以读取 run list/detail，并通过 realtime event 看到进展。

## 13. Translation Plan

研究结果会变成结构化约束，而不是只作为纯自然语言 context。

术语模式：

- `hard`；
- `preferred`；
- `contextual`。

Plan 还可以携带：

- aliases；
- confidence；
- applicable blocks；
- Translation Memory examples。

## 14. Translation Memory

`translation_memory_entries` 保存：

- source / normalized source；
- target；
- target language；
- domain；
- task / subtitle job；
- source kind；
- status；
- quality score；
- usage count。

当前召回策略：

- machine TM 只在同一 Task 复用；
- 跨 Task 需要 `approved`；
- similarity 使用文本归一化和 SequenceMatcher；
- recall 数量受限，避免 prompt 膨胀。

## 15. Translation Validation

当前至少检查：

- empty output；
- number mismatch；
- hard term missing；
- preferred term not used。

阻断错误触发 targeted repair；不需要重新生成整个 batch。

## 16. Dictionary

支持：

- source CRUD；
- entry CRUD；
- lookup；
- import；
- import preset；
- promote。

Dictionary 可以作为 RAG 高优先级本地证据。

## 17. Knowledge Base

支持：

- item list/create/delete；
- import；
- vector rebuild；
- embedding model切换；
- mixed-dimensional historical data。

运行状态页可以看到：

- pgvector version；
- vector column type；
- model/dimension buckets；
- active vectors；
- HNSW present/usable；
- actual search mode。

## 18. Render / Task Queue

Task 自身包含：

- priority；
- queue position。

Subtitle API 支持：

- queue settings；
- task priority/order；
- reorder；
- tick。

RenderJob 单独保存 status/progress/error。

## 19. 审核

发布前审核支持：

- enabled；
- blocked words；
- AI rules。

Task 可经过：

```text
READY_FOR_REVIEW
→ APPROVED
```

## 20. Bilibili

功能：

- Cookie auth；
- auth/me test；
- archive type list；
- type recommendation；
- publish settings；
- publish job；
- default metadata template；
- task-specific publish draft；
- cover/title/reprint policy。

Cookie 不回显明文。

## 21. Social Publisher

平台：

- Douyin；
- Xiaohongshu；
- Kuaishou。

账号来源：

- Web browser login；
- SAU storage_state JSON import。

账号校验状态包括：

- queued；
- checking；
- valid；
- invalid；
- error。

发布状态：

- `submitting`；
- `submitted`；
- `published`；
- `unknown`；
- `failed`。

`unknown` 不自动重试。

## 22. Publish Batch

同一 Task 可以面向多个 target。

PublishBatch 保存：

- expected targets；
- request；
- outcomes；
- cleanup delivery version；
- finish timestamp。

PublishJob 表示单个平台/账号的具体执行。

## 23. Playout

只有 `video_final` 可以加入 ffplayout。

`PlayoutAssetLink` 保存：

- task；
- asset；
- ffplayout channel；
- relative media path；
- transfer mode；
- source checksum。

## 24. Operations Center

Web Operations 页面聚合：

- system resources；
- AI usage；
- pricing；
- provider status/error；
- alerts；
- Agent runs；
- queue/workers。

Alert 支持：

- report；
- automatic scan；
- acknowledge；
- resolve。

## 25. AI Usage

`AIUsageEvent` 保存每个终止 AI request attempt：

- provider/model；
- operation；
- success；
- HTTP status；
- input/output/total tokens；
- latency；
- estimated cost；
- error type/message；
- task id。

## 26. Remote API

外部自动接入当前主要提供：

```text
POST /api/remote/auto/youtube
```

要求：

- Bearer token；
- Idempotency-Key；
- JSON body。

详见 [REMOTE_API.md](REMOTE_API.md)。

## 27. Realtime UI

Topics：

- tasks；
- queue；
- resources；
- agents；
- publishing；
- `task:<uuid>`。

Redis Pub/Sub 是增量通知，不是状态源。

## 28. 存储

容器默认路径包括：

```text
/storage/objects
/storage/work/videoroll
/storage/work/social-publisher
/storage/playout-media
```

Web Settings 可以配置资源保留策略。

系统还支持：

- terminal task resource cleanup；
- workdir cleanup。

## 29. 数据库与迁移

当前 Alembic revisions：

```text
0001_security_architecture
0002_bilibili_upload_progress
0003_task_stop_controls
0004_playout_asset_links
0005_operations_center
0006_agent_runtime
```

Orchestrator 启动时升级 schema 到 head。

## 30. 安全模型

当前主要控制：

- Admin auth；
- service token；
- trusted proxy；
- Remote API bearer token；
- Desktop Grant；
- encrypted platform credentials；
- SecurityAuditEvent；
- role-separated egress；
- non-root app user；
- cap_drop ALL；
- no-new-privileges。

详见 [SECURITY_AUDIT.md](SECURITY_AUDIT.md)。

## 31. 非目标

当前项目不保证：

- 任意网站通用下载；
- 无授权内容分发；
- 绕过 DRM / paywall / platform restriction；
- 任意 Agent shell/code execution；
- 对 `unknown` 发布结果自动重试；
- Redis 丢失后仍保留每一条 realtime notification。

## 32. 一次成功流水线的基本标准

1. Task 状态可追踪；
2. 主要产物都有 Asset 记录；
3. 重试不会无条件重复外部副作用；
4. 翻译过程有 trace/checkpoint；
5. 发布结果区分确定失败与未知结果；
6. 服务重启后能从 PostgreSQL 恢复业务状态。
