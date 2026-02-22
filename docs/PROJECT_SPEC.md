# VideoRoll（合规版）项目规格：模块化视频处理流水线

> 目标：对**你拥有版权/已获授权/允许再分发**的视频，实现“内容接入 →（可选下载）→ 语音识别/翻译 → 生成字幕 →（可选）压制/封装 →（可选）投稿到哔哩哔哩”，并通过网页进行任务管理、人工审核与自定义投稿信息。
>
> 非目标：自动抓取/搬运他人热门内容、绕过平台规则、批量转载、规避风控。

---

## 1. 总体拆分：三大模块 + 一个编排层（Orchestrator）

### 1.1 模块列表与边界

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

### 1.2 两种落地方式（推荐先 1 后 2）

**方式 A：Monorepo + 进程级解耦（推荐起步）**
- 同仓库放四个服务/包，但每个模块都有自己的入口、配置、依赖、接口。
- 好处：开发快、改动集中；上线时也能拆分部署。

**方式 B：真正微服务（HTTP/RPC + 独立部署）**
- 各模块完全独立仓库或独立镜像，由 orchestrator 通过 HTTP 调用。
- 好处：边界最清晰；坏处：前期维护成本高。

---

## 2. 统一“产物契约”（Artifact Contract）：模块解耦关键

模块之间只通过两类东西交互：
1) **DB 里 task 的状态与元信息**
2) **对象存储里的产物 key（S3/MinIO）**

### 2.1 存储 Key 规范（示例）

建议按 `tenant/account/task_id` 分层，便于多账号与隔离：

- `raw/{task_id}/video.mp4`（原视频）
- `raw/{task_id}/metadata.json`
- `work/{task_id}/audio.wav`
- `sub/{task_id}/segments.json`（ASR 分段结构化结果）
- `sub/{task_id}/subtitle_src.srt`
- `sub/{task_id}/subtitle_zh.srt`
- `sub/{task_id}/subtitle_zh.ass`
- `final/{task_id}/video_burnin.mp4`
- `final/{task_id}/video_softsub.mkv`
- `final/{task_id}/cover.jpg`
- `meta/{task_id}/publish_meta.json`
- `meta/{task_id}/publish_result.json`
- `logs/{task_id}/stage_{name}.log`

### 2.2 产物对象最小字段（建议）

每个产物在 DB 的 `assets` 表里记录：
- `task_id`
- `kind`（枚举：video_raw/audio_wav/subtitle_srt/subtitle_ass/video_final/cover/log 等）
- `storage_key`
- `sha256`（可选但强烈建议）
- `size_bytes`
- `duration_ms`（视频/音频）
- `created_at`

---

## 3. 任务状态机（由 Orchestrator 维护）

建议把任务拆成“内容接入/下载”、“字幕制作”、“发布”三段，每段内部再细分 stage；每个 stage 都是**幂等**、可重试、可断点续跑。

### 3.1 建议状态枚举（可按需要裁剪）

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

### 3.2 幂等/重试原则

- 每个 stage 以 `task_id + stage_name` 作为幂等键；产物存在则跳过或做一致性校验。
- `FAILED` 不代表终止，保存 `error_code` 和 `is_retryable`，支持手动/自动重试。
- 发布阶段需要额外幂等：若已拿到 `aid/bvid`，重复提交应改为轮询/对账，而非再次投稿。

---

## 4. subtitle-service（字幕/翻译模块）设计（重点）

### 4.1 支持的业务场景

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

### 4.2 内部流水线（可拆分 job）

**S1 音频提取（FFmpeg）**
- 输入：视频文件
- 输出：`audio.wav`（建议单声道 16k）

**S2 ASR（语音识别）**
- 引擎可插拔（本地 ASR / faster-whisper / 其他）
- 输出：`segments.json` + `subtitle_src.srt`

**S3 翻译（可选）**
- 输入：`segments.json` 或 `subtitle_src.srt`
- 输出：`subtitle_zh.srt`
- 可插拔 provider：LLM / 翻译引擎；支持 `glossary`（术语表）和 `do_not_translate`（专有名词表）。

**S4 字幕排版与对齐（强烈建议做）**
目标：可读性与节奏一致
- 断句：按标点/停顿/最大行宽
- 约束：每条字幕最短/最长时长、CPS（每秒字符数）上限
- 输出：`subtitle_zh_fixed.srt` + 可选生成 `ASS`（样式）

**S5 产物输出**
- 仅字幕：写入 `sub/`
- burn-in：FFmpeg + ASS（推荐用 ASS 样式作为输入，效果更好）
- soft-sub：容器封装（建议 MKV，MP4 的字幕支持更有限）

### 4.3 对外接口（建议：HTTP + Job 模式）

#### 4.3.1 创建字幕任务
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

#### 4.3.2 查询任务状态
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

### 4.4 “只出字幕 vs 压制字幕”怎么落地（推荐的参数化）

在同一条 API 里用 `output.render` 控制即可：
- 只出字幕：`burn_in=false, soft_sub=false`
- 硬字幕：`burn_in=true`
- 软字幕：`soft_sub=true`
- 两者都要：都设为 `true`

> 产物 key 不要复用同名，避免幂等冲突；建议 burn-in/soft-sub 输出到 `final/`。

---

## 5. youtube-ingest（YouTube 接入/抓取模块）设计（与发布解耦）

> 合规核心：**只允许白名单源**（你自己的频道/你已获授权的频道或播放列表/CC 许可明确的源），并把许可信息写入任务，支持人工补证据。

### 5.1 能力清单

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

### 5.2 对外接口（示例）

#### 5.2.1 手动入队单条视频
`POST /youtube/ingest`
```json
{
  "url": "https://www.youtube.com/watch?v=xxxx",
  "license": "own",
  "proof_url": null,
  "download": false
}
```

#### 5.2.2 扫描频道/播放列表
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

### 5.3 与 subtitle-service 解耦的点
- youtube-ingest 只产生 `raw/metadata.json`（以及可选 `raw/video.mp4`）
- subtitle-service 不关心 YouTube，只关心 `raw/video.mp4` 是否存在

---

## 6. bilibili-publisher（B 站上传投稿模块）设计（只接 final 包）

### 6.1 输入契约：Publish Package

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

### 6.2 输出：发布结果

```json
{
  "state": "submitted",
  "aid": "123",
  "bvid": "BVxxxx",
  "response": { "raw": "..." }
}
```

### 6.3 关键工程点（不涉及绕过风控）
- 分片/断点续传：上传过程要可恢复（网络抖动常见）
- 速率限制：同账号投稿间隔、每日上限（避免触发平台限制）
- 凭据安全：cookie/csrf 不在日志里打印，DB 加密存储
- 幂等：同 task 重试 publish 不应重复投稿；优先对账/轮询状态

---

## 7. Orchestrator（编排层）要做的“产品能力”

### 7.1 Web 管控台核心功能
- 任务列表：状态筛选、搜索、批量重试/取消
- 任务详情：查看每 stage 的日志与产物；字幕文件在线预览/编辑；投稿 meta 编辑；审核通过/驳回
- 模板中心：标题/简介/标签/分区/动态文案模板，支持变量（如 `{source_title}`、`{publish_date}`）
- 账号与凭据：B 站凭据录入与轮换提醒；权限控制
- 系统设置：并发数、重试策略、队列开关、通知（Webhook/邮件等）

### 7.2 队列编排建议
如果用队列（推荐）：
- `queue:ingest`（youtube-ingest 相关）
- `queue:subtitle`（subtitle-service 相关）
- `queue:render`（ffmpeg 压制）
- `queue:publish`（bilibili-publisher）

每个队列可独立扩容 worker，且失败不影响其他队列。

---

## 8. 最小可运行里程碑（建议）

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

---

## 9. 你需要提前确定的 5 个关键选项（决定代码形态）

1) 技术栈：**Python（FastAPI + Celery）** 还是 **Node（NestJS + BullMQ）**
2) ASR：本地（faster-whisper）还是云（可插拔）
3) 翻译：是否需要、是否需要术语表、是否需要双语
4) 字幕输出：只字幕 / burn-in / soft-sub / 全要
5) 发布策略：全自动还是必须人工审核后才能投稿

