# VideoRoll P2：中文配音及 B 站双分 P 设计与实施记录

## 投稿后 24 小时清理与字幕完成后的手动配音

- 投稿所有目标成功后的清理不再立即执行：`publish.cleanup` 的持久化 Outbox 设置 `available_at = publish_batch.finished_at + 24 小时`。共享 Dispatcher 在截止时间前不发送清理任务；对于升级前已经发出的 Celery 消息，清理 Worker 再次检查时间并延后执行。
- 周期保留策略仍由「存储资源」的 `asset_ttl_days` 管理；生产建议设为 **1 天**。周期清理会跳过包含排队、语音合成、对齐或混音等活跃 DubbingJob 的任务；删除范围包括手动配音片段缓存 `dub/{task_id}/tts/`。
- 手动中配可直接使用已成功字幕任务的最终句段文件，选择原视频 `video_raw` 或已渲染的 `video_final`，无需先生成 P1。已经投稿的 Task 只要原视频仍在也允许手动创建中配；取消状态的 Task 仍禁止操作。
- 选择原视频配音时，输出 `source_video_key` 指向原视频，同时 `subtitle_video_key = null`，明确该作品不是 P1，不能假冒已完成的 P1 双分 P 直接投稿。后续渲染与投稿需要用户手动处理。
- **边界**：此前已经被清理删除的原始素材不能自动恢复；资源只保证至少 24 小时等待，活跃 DubbingJob 可能延长保留，且定时清理周期会使实际完成清理时间略晚于 24 小时。模型安装与独立 Qwen3-TTS Worker 的运行仍为实际合成前提。


## 第四批：中文配音模型管理

配置侧新增「设置 → 中文配音 / 模型」，提供以下功能：

- 官方 CustomVoice 0.6B、1.7B 两个可选推理模型（服务端严格允许名单，不接受自定义下载 URL 或服务器文件路径）。
- Hugging Face / ModelScope 下载源、模型状态、已下载字节数量、失败说明；单模型下载互斥，取消或进程异常后再次点击「继续下载」会复用已有权重文件。
- Qwen3-TTS Tokenizer 作为共同依赖优先下载；模型成功下载和权重校验之后才标记安装完毕。
- 模型、固定中文音色、设备 (cuda:0/cuda:1/cpu) 的默认推理选择持久化到数据盘；手动任务页读取所选模型并拒绝未安装模型。
- 推理 Worker 只从受管的本地模型目录加载，不会在合成任务开始时从 Hugging Face 自动拉取权重。对应视频片段缓存仍使用模型的稳定仓库 ID 计算哈希。
- API：`GET /settings/dubbing`、`PUT /settings/dubbing`、`POST /settings/dubbing/models/download`；管理员认证沿用 Orchestrator 的统一保护。
- 下载 Python 应与推理、字幕 Python 环境隔离。示例环境变量为 `DUBBING_MODEL_ROOT=/opt/videoroll/data/models/tts` 和 `DUBBING_DOWNLOAD_PYTHON=/opt/videoroll/venvs/dubbing-download/bin/python`。在此 venv 内安装 `huggingface_hub` 与 `modelscope`。
- **注意**：下载按钮只下载文件，并不安装 PyTorch/qwen-tts 或启用 TTS Worker；这些仍须单独配置与真实 V100 推理验收。



状态：**开发中，独立分支 `feature/dubbing-multipart`**。本阶段禁止直接部署生产。

## 第三批：手动模式操作界面及故障恢复

在开发分支 `feature/dubbing-multipart` 新增：

- **手动 UI**：任务详情 →「字幕 / 渲染」→「中文配音 · 手动模式」可选成功的 SubtitleJob、P1 `video_final`、CustomVoice 模型及中文音色，创建 DubbingJob。按 5 秒周期刷新状态，提供失败信息、WAV 音轨和 P2 MP4 下载。只有任务存在相应的成功字幕和 P1 资产才能开始配音。
- **单句改写 / 试听**：`GET /tasks/{task_id}/dubbing_jobs/{job_id}/segments` 分页列出原字幕和口播文案；`PUT .../segments/{index}` 仅允许未运行的 queued/failed Job 改写 `speech_text`，不改原字幕；`GET .../segments/{index}/audio` 返回仅与当前模型、文本和音色匹配的缓存 WAV。前端支持改写和试听，然后断点重试。过长 TTS 仍不会自动 AI 缩写，但可人工局部缩写而不重跑整个翻译过程。
- **显式恢复**：`POST /tasks/{task_id}/dubbing_jobs/{job_id}/retry` 只允许 failed / queued / 租约过期的中配 Job 重新调度；活跃租约、已成功/已取消作业均拒绝恢复。SQLite 的 naive DateTime 和 PostgreSQL 的 aware timestamp 均按 UTC 处理，避免时间戳类型冲突。
- **进度 / 清理**：逐句 checkpoint、`aligning`、`mixing` 阶段实时登记；发现源 Task 已停止时拒绝继续提交音频和成品。临时 WAV 文件名唯一化，减少并行恢复时文件冲突。
- **手动 B 站双分 P**：投稿页提供单独开关（默认关闭）。开启时前端使用同一成功 DubbingJob 的 `subtitle_video_key` 和 `video_key` 配置有序双分 P，Bilibili Publisher 还会独立校验 P1/P2 同任务资产和成功配音状态。未勾选保留旧单 P 行为。**真实 B 站投稿尚未验证，请勿直接对外发布测试稿。**

### 部署前验收边界

当前开发阶段的自动化测试只使用离线模拟 TTS 和 FFmpeg。首次生产试跑前必须：

1. 在生产代码历史与开发分支对齐后再合并、部署；**生产机当前不是这一分支**。
2. 用独立 `/opt/videoroll/venvs/dubbing` 安装 Torch CUDA + qwen-tts、soundfile、Hatchet 等依赖；下载 CustomVoice 模型；在 V100 上进行真实 FP16 + SDPA 单句 smoke test。
3. 先让字幕任务和 P1 正常完成，再在管理页面手动运行中配，确认 1–3 分钟测试视频的声画同步及质量。检查异常超时句子是否能人工缩短并继续。
4. 初次双分 P 投稿时使用无公开影响的测试稿，人工确认 BVID、两个 CID 顺序、标题、审核状态及新手动模式是否绕开已有定时自动投稿。
5. 自动模式默认**不启用配音**，P1/P2 全自动就绪门禁及自动发布衔接仍在待开发阶段。



## 第二批实现进展（开发分支，未部署）

本次新增了以下可执行模块：

- `migrations/versions/0011_dubbing_jobs.py` / `DubbingJob`：持久化任务、独立 lease、进度、失败与断点续跑。新增 `dubbing_audio` 和 `video_dubbed` 资产枚举；字幕渲染成品仍使用 `video_final`。
- `apps/dubbing/qwen_tts.py`：延迟加载官方 `Qwen3TTSModel.generate_custom_voice`，支持 `0.6B/1.7B CustomVoice` 的中文固定音色。Tesla V100 使用 FP16 + SDPA，**尚未实机验证模型能否成功加载和语音质量**。
- `apps/dubbing/pipeline.py`：消费最终中文 JSON，保留独立 `speech_text`；以文本、模型和音色哈希缓存单句语音，失败重启不强制全部重合成。
- `apps/dubbing/audio.py`：逐段 PCM 重采样、调整语速、静音补齐，流式生成完整音轨，按原视频的时间轴封装，不将长音频全部读入内存。
- `apps/dubbing/job.py`：状态更新/租约/续跑、音轨和 P2 的独立资产写入；模型推理前提交并关闭事务，减少 PostgreSQL idle-in-transaction 故障。
- `workflows/dubbing_job.py`、`dubbing_worker.py`：专用 `dubbing` Hatchet Worker pool，保持模型与字幕、渲染节点隔离。运行前需要独立的 Python 虚拟环境。
- Orchestrator 的 `POST /tasks/{task_id}/actions/dubbing` 和 `GET /tasks/{task_id}/dubbing_jobs`，可创建、查询配音任务；只有源翻译完成且 P1 视频资产存在才能创建任务。
- Bilibili 投稿处理检查分 P 资产归属及成功的 P2 DubbingJob，防止提交了无效或其他任务的视频。
- 发布清理进程保留中文音轨和 P2 视频，并在配音运行中暂停清理。

### 手动接口示例

在 Orchestrator 已认证 API 上提交任务（**前提：P1 已渲染并且有对应成功的 subtitle_job**）：

```json
{
  "subtitle_job_id": "<成功的 SubtitleJob UUID>",
  "subtitle_video_key": "<当前任务已保存的 P1 视频存储键>",
  "operation_key": "p2:initial",
  "voice": "Vivian",
  "model_path": "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
  "retain_original_audio": false
}
```

GET 查询 `/tasks/{task_id}/dubbing_jobs`。配音成功后返回 `output.audio_key` 与 `output.video_key`，下载使用已有的 Task Asset API，而不是传生产机绝对路径。

发布时经既有多平台发布接口向 Bilibili 传入两个分 P（先验证两个资产已生成）。当未显式传入 `video_parts` 时，不改变原单 P 流程。

### Native Worker 运行前置

新增角色 `videoroll@dubbing-workflow-worker.service` 对应脚本角色 `dubbing-workflow-worker`；为避免安装额外 GPU 依赖影响现有服务，**它不会被 videoroll.target 自动启用**。独立 Python 3.12 环境推荐置于 `/opt/videoroll/venvs/dubbing`，其中必须安装 VideoRoll 本体、Hatchet SDK、PyTorch、`qwen-tts`、`soundfile` 等；FFmpeg/ffprobe 需要能执行。模型需要由服务账户读取，工作盘和 `STORAGE_ROOT` 应可写。在 V100 上先测试单句生成/峰值显存，再开启任何后台 Worker。

### 尚未完成（绝不可声称完整上线）

1. **自动模式**：自动生成 DubbingJob、等待双产物全部完成、原有 after-render 发布改为单 BV 双分 P；当前仅提供显式任务入口。
2. **前端**：自动设置页的中配开关、音色配置、任务详情试听/重新生成、双文件下载按钮。
3. **真实 Qwen 推理**：尚未在目标 V100 完成模型安装、FP16/SDPA 兼容性或长视频速度验证。
4. **人声分离**：基础混音只降低整条原音轨，不能只保留环境声；Demucs 尚未开发。
5. **重放与恢复**：离线缓存/DB 幂等已经测试，Hatchet 中断/机器关机恢复以及真实 B 站分 P 投稿仍需端到端测试。
6. **发布安全**：启用双分 P 前必须确保自动发布不会提前提交 P1；现阶段不要为生产任务自动开启中配。

### 测试

- SQLite DubbingJob 从创建、生成完整音轨/P2、写资产、二次调用不重新执行 TTS 的离线端到端测试。
- FFmpeg 原视频和生成 P2 的视频编码、音频格式、总时长测试。
- 配音时间槽、超时拒绝、音频拼接静音、哈希缓存测试。
- Bilibili 两分 P 提交顺序、CID 不重复、单稿一次请求及既有投稿行为的回归测试。



## 目标与约束

每个翻译任务只执行一次下载、ASR、术语检索和中文翻译，最终产出：

| 分 P | 产物 | 内容 |
| --- | --- | --- |
| P1 中文字幕版 | `subtitle.mp4` | 原音频 + 中文烧录字幕（沿用现有 RenderJob） |
| P2 中文配音版 | `dubbed.mp4` | **复用 P1 的视频码流** + 配音/混音后的中文音轨 |

上传时按顺序向一次 B 站投稿提交 `videos=[P1, P2]`；**不能按两个 BV 分别投稿再加合集**。已有同稿发布逻辑和作者合集逻辑仍作用于一个 aid/bvid。

不开启中配时，现有单 P 上传、单 RenderJob 和下游自动流程必须保持不变。

## 已完成的第一批最小可验证代码

- B 站发布请求可选 `video_parts: [{video: {type: storage, key: ...}, title: ...}, ...]`，严格限定 P1/P2 两个、不同 storage key，首 P 须等于兼容字段 `video.key`。
- Publisher 在数据库 `meta_json` 中保存顺序，并按两文件**总字节数**更新上传进度。
- 每个分 P 单独调用既有的上传文件接口，之后**仅调用一次** `add_archive`，传两个已上传视频的 filename/cid/title。投稿前校验两个 cid 不重复。
- `dubbing/timing.py`：基于原始视频的 start/end 和音频实测时长计算时间槽；允许有限静音借位与变速，超限抛出 `DubbingTimingError`，要求缩短口播文案，绝不截断语音或拖慢画面。
- `dubbing/mux.py`：P2 对 P1 视频使用 `-c:v copy`，音频 AAC 编码；支持替换原声或可选低音量混合原声，并按原视频总时长封装。注意低音量混合**不等于**人声分离。

**注意：这尚不是完整的自动中配功能。** 数据表、模型推理、分句试听、音频时间线渲染、两个产物的工作流会在下一切片接入。

## 下一切片：DubbingJob（必须先于自动启用）

### 任务与数据库

推荐新表 `dubbing_jobs`：`id`、`task_id`、`subtitle_job_id`、`source_segments_key`、`voice_profile_json`、`status`、`progress`、`lease_owner`、`lease_until`、`heartbeat_at`、`request_json`、`output_json`、`error_message`、`created_at`、`updated_at`。

新增 `AssetKind`：`dubbing_segments`、`dubbing_audio`、`video_dubbed`。不能简单复用 `video_final` 的最新记录，否则自动发布会错误选择 P2 为 P1。

状态：`queued → preparing → synthesizing → aligning → mixing → succeeded`；并支持 `failed`、`canceled`。每片段以文本、模型版本、voice、参考音频及生成参数生成稳定缓存键。重试必须能够从成功片段继续。

尽量沿用既有 Hatchet 任务、DB 租约、幂等执行、心跳、失败检查点，不在渲染 Worker 中加载大模型。

### 生成与对齐

数据来源：`final_subtitle_segments_key`（最终字幕 JSON），为每个语义段分别保留：

- `subtitle_text`：阅读用中文（不变）
- `speech_text`：适合口播、可缩写的中文
- `start/end`：原视频坐标轴
- `speaker_id / voice_id`
- `audio_key / measured_audio_duration / playback_speed / alignment_status`

初版 TTS 固定中文音色；配置提供可替换的供应商适配接口。语音生成不能把 `字幕文本 == 口播文本` 当作强制前提。句子过长时先让 Agent 改写或局部重生成，再进入异常人工处理；长视频不能靠整体变速解决。

独立音频装配成完整静音填充的单声道音轨，采样率/时间戳统一；不允许前段误差向后累积。边界进行试听及响度检查。人声分离（Demucs）属于高级可选节点，初版原声混音会连原语音一并混入，必须在 UI 如实标注。

### 渲染和上传门禁

建议最终使用：
`SubtitleJob → P1 RenderJob` 与 `SubtitleJob → DubbingJob → P2 MuxJob` 两支并行；
`PublishReady` 同时等待 **P1 成功 + P2 成功 + 元数据审核通过**。

发布输入按顺序构造：

```json
{
  "platform": "bilibili",
  "video": {"type": "storage", "key": "render/<task>/subtitle.mp4"},
  "video_parts": [
    {"video": {"type": "storage", "key": "render/<task>/subtitle.mp4"}, "title": "P1 中文字幕版"},
    {"video": {"type": "storage", "key": "render/<task>/dubbed.mp4"}, "title": "P2 中文配音版"}
  ]
}
```

- 中配开关关闭 → 原有单 P 发布。
- 开启且 P2 尚未完成 → 不允许提交 P1 单 P；防止部分成功破坏投稿规划。
- P2 失败 → 显示失败/人工选择降级，默认**不自动降级为单 P**。
- 两个文件必须属于同一个 task、不同 storage key 且上传前确实存在；校验音轨和视频时长。
- 投稿前保证有序；提交中断或结果不确定时进入 `unknown` 状态，防止自动重复创建 BV。
- 不要因预览/音频试听改动原版字幕产物；清理程序不可提前删除任一仍被引用的分 P。
- 同时投稿其他平台时，不应向不支持多分 P 的平台透传 `video_parts`。

### UI

自动模式与手动模式添加：配音开关、TTS 服务地址/模型/固定音色、混音方式、P2 字幕是否保留。任务详情显示配音状态、语音片段成功数量、试听与局部重生成、P1/P2 文件下载和各自时长。

### 验收标准

1. 关闭中配不回归，只有 P1 的旧投稿请求不变。
2. 模拟中配两产物时，Mock 只观察到一次 `add/v3`，且 `videos` 数组有两个正确顺序的 cid。
3. 模拟第二文件上传失败时没有提交投稿请求；重试遵循 `unknown` 保护。
4. 20+ 分钟视频的配音 TTS 分片允许故障恢复，不重复 ASR、翻译或重新渲染 P1。
5. P2 FFmpeg 媒体探针显示视频 codec 与 P1 相同，音频流为 AAC，总时长一致。
6. 单句配音越界时拒绝音频截断、拒绝视频整体变速。
7. 真实 B 站测试前先使用私密测试稿，提交后核对 BV 的两页 cid/P 顺序和分 P 标题，再考虑开启自动发布。

## 集成风险

- 生产机源码曾报告提交 `92eb843`，开发分支基于另一台开发机 `c3f529e`，两台机器未完成同一 Git 历史确认；部署前必须对齐提交并检查最近字幕修复。
- 当前 P2 仅是产品分 P 名称，源码中的 NVENC 编码器预设 `p2` 与该功能无关。
- V100 显存已有其他模型占用，Qwen3-TTS 推理必须独立探测可用显存、计算精度和注意力实现，不可默认套用 BF16/FlashAttention-2。

## 配音生产可靠性第二阶段（2026-10-14）

### 上游参考

- [pyVideoTrans 架构和复审流程](https://en.pyvideotrans.com/yuanli)：音频与字幕编辑、逐句重配、SpeedRate 配音/视频对齐。VideoRoll 保留源视频时长，**不会偷偷截断语音或随意减速视频**。
- [pyVideoTrans 音画同步说明](https://doc.pyvideotrans.com/subtitle-sound-alignment)：语音加速、视频减速及二者协作模式。VideoRoll 对超时句子先停止在待修订状态，以避免 334 句全跑后统一失败。
- [Qwen3-TTS 官方批量 CustomVoice 示例](https://github.com/QwenLM/Qwen3-TTS/blob/main/examples/test_model_12hz_custom_voice.py)：1.7B 的语气指令及多句批量文本支持，0.6B CustomVoice 不支持 instruct。

### 主要更改

- 新 DubbingJob 生命周期：queued → preparing → synthesizing → preview_ready (首句试听确认) → queued → synthesizing → aligning → mixing → succeeded；或 pause_requested → paused → retry；cancel_requested → canceled；超长语音 → needs_review → 改稿 → retry。
- 暂停/取消为**协作式安全停止**：当前正在执行的单句或批次推理结束后检查控制信号；不会 SIGKILL GPU Worker 或把源 Task 标记 canceled。可恢复缓存，允许多次断点继续。用户不应将此误以为立即中止 GPU 内核。
- 正式生成前必须先完成首句试听。试听只产生首句 WAV，不封装全视频；确认后复用缓存继续，避免单纯点击即生成数百句。
- 每句 WAV 产生后即检查是否适合下一句字幕起点、0.5s 扩展及最大 1.25x 语速；不符合便进入 needs_review，并提供具体的句号、时长、槽位和所需倍速。
- 配音页面提供“AI 精简口播建议”，按下后调用当前已配置的兼容 OpenAI 翻译接口生成**候选文案**。结果不改变中文字幕、不自动保存或自动开始合成，必须由用户确认并保存；语义准确性仍需人工检查。该功能依赖配置了可用 API Key 的翻译模型。未经设置时返回明确错误。
- CustomVoice 开放 instruct（只针对 1.7B）、temperature、top_p 及可选 batch_size=1/2/4。V100 默认 1；批量模式需进一步基准测试及注意显存峰值。生成结果记录 batch 数、生成音频秒数及墙钟耗时。
- 音频缓存 schema2 将模型标记版本、文本、语音角色、instruct、temperature、top_p 纳入哈希；旧任务继续使用原有 schema1，避免升级强制丢缓存。并发创建通过锁定同一 Task 行并二次去重，杜绝不同 operation_key 同时运行两个配音任务。
- 模型安装但 Worker 关闭时，API 返回 503 并禁用页面首句试听；不能提交无人执行的队列。恢复时也检查 Worker 是否存在。
- 源视频处理模式为 replace、original_low 和 background_only。最后一项由 Demucs htdemucs 分离「no_vocals」伴奏轨，再与中文配音混音；要求单独安装 Demucs 和模型，未安装时 UI 禁止选择并返回 503，不会用“降低原声”冒充人声分离。
- 暂停、首句预览和待修订状态最多保护相关素材 7 天；运行中的状态持续保护。超过保留期限可能由存储保留/投稿清理流程清除素材；配音已完成及已取消的任务依既定保留策略处理。

### 发布边界

- 新状态使用现有的 DubbingJob.status 字符串字段、request_json 和 output_json，不引入数据库迁移。
- 生产环境先只部署后端与前端，并**保持当前配音 Worker 禁用**，不会自动恢复之前被取消的 334 句任务。
- 仍需要短片真实试听验证中文配音自然度、Demucs 分离音质，以及 batch_size 2/4 在 V100 上的实际性能；不应把这些尚未测试的能力标为已完成的音质性能验收。

## CosyVoice 3（实验性引擎，2026-10-14）

本功能在独立分支验证，通过真实 V100 Base/RL 短句生成之后才能作为生产可选项发布。不能依据单元测试通过即承诺模型可用。

- **模型 ID**：`cosyvoice3:base` / `cosyvoice3:rl`；二者映射到同一官方权重目录 `Fun-CosyVoice3-0.5B-2512`，其中 `llm.pt` 和 `llm.rl.pt` 并存。RL 通过私有只读链接视图替换被加载的 `llm.pt`，不移动或复制共享基础权重。
- **官方源码**：`https://github.com/QwenAudio/CosyVoice`；固定外部部署目录 `/opt/videoroll/external/CosyVoice`；需要初始化 Matcha-TTS 子模块。
- **运行时**：隔离 Python 3.10 虚拟环境 `/opt/videoroll/venvs/cosyvoice3`，不覆盖 Qwen 3.12 环境。模型仅由可信 Hatchet Worker 通过本机 stdin/stdout JSON 协议调用；不会向公网开放 CosyVoice 官方演示服务。
- **参考声音**：从该任务已有原视频/P1 指定时间区间提取单声道 16kHz PCM WAV，支持 3–30s，生成音频哈希及关联任务的私人声音库；创建时必须明确确认授权。浏览器允许试听；正在运行/待审核任务引用的参考音频不能删除。上传前应审阅声音克隆授权与源视频内容。
- **引擎能力**：`cross_lingual`（推荐英文声音说中文）、`zero_shot`（需要与参考声音相对应的转录文本）、`instruct`（需要指令）；非流式 `speed=0.8–1.25`，默认 1.0。优先 PyTorch FP32 和 `load_trt=False, load_vllm=False`。
- **工作流**：复用现有字幕、首句试听、暂停/取消、续跑、语速校验、音频归一化和 MP4 混音逻辑。各片段缓存依据引擎、Base/RL、参考音频 sha256、语速、模式及文本构建独立键，避免误复用 Qwen 语音。官方 CosyVoice 3 不是已经完成多句并行推理的证明，现阶段固定 `batch_size=1`。
- **质量校验**：子进程音频必须是 24kHz、有限数值、非静音、峰值合理的完整 WAV，音频输出采用临时文件后原子替换。系统不擅自切除超出字幕时间槽的台词，进入 `needs_review`，用户改稿后续跑。
- **依赖**：官方锁定部分老版 `torch==2.3.1`、`transformers==4.51.3` 和 `openai-whisper==20231117`，新版 Torch 与旧版 Whisper 的 Triton 上限不兼容。应保持隔离，按现场 V100 验证。
- **后续性能/音质验收**：分别执行 Base/RL 参考声音克隆、音频自然度/术语、首句试听与整段封装；对比 Qwen3-TTS 的 RTF、显存和失败率。不通过则继续保留现有 Qwen 默认引擎，绝不批量启动旧任务。

## 统一配音服务（Qwen3-TTS + CosyVoice3）

VideoRoll 对所有 TTS 模型仅部署**一套统一内网 API 服务** `videoroll@dubbing-service.service`，端口 `127.0.0.1:8040`（不得绑定公网），入口 `videoroll.apps.dubbing.serving:app`。它使用轻量的 Core Python 解释器，不直接导入任何 CUDA 模型。配音的 Hatchet 持久化任务由既有 `videoroll@dubbing-workflow-worker` 运行，但所有语音模型加载与 GPU 推理均统一经认证 HTTP 客户端 `RemoteSpeechSynthesizer` 调用本服务。**外部客户端仍只能访问带鉴权的 VideoRoll Orchestrator，不可以直接访问模型服务。**

- 服务采用单一 V100 推理槽（`SpeechEnginePool` 互斥调度）。按需加载 Qwen3-TTS 0.6/1.7B 或 CosyVoice3 Base/RL，后续同配置任务持续复用 GPU 已加载模型；切换引擎前退出旧 CUDA 子进程释放显存。GPU 服务保持空闲时不预加载全部模型。
- Qwen 使用隔离 `/opt/videoroll/venvs/dubbing/bin/python` 的 `qwen_bridge.py`；CosyVoice3 使用隔离 `/opt/videoroll/venvs/cosyvoice3/bin/python` 的 `cosyvoice3_bridge.py`。共享的内部协议为 NDJSON、WAV 文件和运行状态，不允许外部网页上传任意路径。
- 推理接口 `POST /v1/synthesize` 支持 1–4 条 Qwen 语音或单句 CosyVoice3；内部凭证 `X-VideoRoll-Token` 取自仅 `videoroll` 服务账户可读的文件 `/opt/videoroll/config/dubbing-service.token`。`GET /health` 仅返回公开运行概况，`GET /v1/status` 返回已加载模型、是否繁忙、成功生成句数及故障次数。全局设置页面显示这些指标。
- 配音任务需要**工作流 Worker + 推理服务两者均在线**才能提交；模型未安装、CosyVoice 依赖没装完时不得创建不可执行的任务。模型权重和现有任务缓存不随子进程切换而删除。
- 请求 WAV 输出路径限制在 `/DATA/videoroll/data/storage/work` 内。参考声音 ID/哈希逐次核验，模型从服务器受控目录加载。协作式暂停/取消在当前句后检查状态，服务故障由 Hatchet 的持久化任务接管重试，不承诺立即终止正在执行的单句 GPU 内核。
- 由 Root 执行 `scripts/native/provision-dubbing-token.sh` 生成持久凭证，然后使用 systemd `enable --now videoroll@dubbing-service.service`。在启动新的统一服务后重启 `videoroll@dubbing-workflow-worker.service` 使其改用 HTTP API；部署不得停掉当前渲染 Worker。
- 当模型包安装或 CosyVoice3 V100 真实音质验收尚未完成时，推理服务可以先为 Qwen 提供生产能力；CosyVoice 选项展示但明确标记 `runtime_ready=false` 并禁止提交。
