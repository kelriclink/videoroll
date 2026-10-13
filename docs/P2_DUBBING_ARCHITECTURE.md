# VideoRoll P2：中文配音及 B 站双分 P 设计与实施记录

状态：**开发中，独立分支 `feature/dubbing-multipart`**。本阶段禁止直接部署生产。

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
