# VideoRoll P2：中文配音及 B 站双分 P 设计与实施记录

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
