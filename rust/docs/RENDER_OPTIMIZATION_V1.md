# Rust 原生字幕渲染优化 V1

日期：2026-11-26

## 目标与兼容原则

将 Python `subtitle_service.processing.render_burn_in()` 的成熟 Intel VAAPI 字幕流水线迁入 Rust `media::ffmpeg`。保留原 RenderProfile 的 codec/CRF/preset 与音频映射语义；主视频和透明字幕层分开处理。优化路径能力不足时逐级回退，不因 ASS 特性复杂而丢失字幕或任务。

## GPU 流水线

1. FFprobe 读取视频尺寸、时长、平均/标称帧率、pixel format 和 bit depth。
2. 以 `INTEL_GPU_RENDER_DEVICE` 初始化 VAAPI；通过单帧 preflight 判断硬件解码是否可用。
3. 硬件解码可用时，主视频留在 VAAPI surface；否则 CPU 解码后上传 NV12/P010。
4. 常规底部 ASS 尝试按实际样式、字号、outline、margin、`\N` 行数及 `{\rStyle}` reset 计算透明字幕 band。位置、移动、绘图、裁切、显式 `\an` 等复杂 ASS 不裁剪，使用全帧透明 overlay。
5. 静态 band ASS 的 overlay plane 最多 15 FPS；动态/动画 ASS 保持源帧率。
6. 透明层通过 libass rasterize、`format=bgra,hwupload`，由 `overlay_vaapi` 合成；主视频不经历 GPU→CPU→GPU 的全帧往返。
7. AV1 源位深大于 8 bit 时使用 P010 并保留 10-bit 输出；H.264 仍使用 8-bit NV12。其他 codec 遵循现有 Rust profile 支持范围。

## ASS Event Cache

只对静态 ASS、且 band 可安全裁剪的字幕启用。按 Dialogue 起止时间生成毫秒级事件边界；每个视觉状态只 rasterize 一张透明 PNG，ffconcat VFR timeline 提供精确 PTS，VAAPI framesync 在状态间重复最后一帧。

准入条件与 Python 一致：

- 最多 4096 个状态边界；
- 输出至少 90 帧；
- `state_count / output_frame_count <= 0.20`；
- PNG 缓存总量不超过 256 MiB；
- 不缓存 transform、move、fade、karaoke 或 Dialogue Effect 动画；
- EOF 状态不生成额外边界/帧。

## 回退链

- Event Cache 构建或合成失败 → 实时 VAAPI overlay；
- 动态/复杂 ASS → 实时 VAAPI overlay（必要时全帧）；
- `overlay_vaapi` 不存在或不兼容 → CPU libass 全帧合成后上传，仍尝试 VAAPI 编码；
- VAAPI 解码失败 → CPU 解码 + GPU upload/overlay/encode；
- GPU 编码/合成整体失败 → CPU libass + 软件编码。

每次渲染将 pipeline、decode mode、source/output bit depth、overlay FPS、event-cache 状态/字节/耗时和 fallback reason 写入 stage result 的 `render_metrics`，便于识别“GPU 编码但 CPU 合成”的降级情况。

## 真实硬件验收（link / Intel Arc A380）

使用 `/dev/dri/renderD128`，`vainfo` 可用，FFmpeg 有 `overlay_vaapi`、`ass`、`h264_vaapi`、`av1_vaapi`：

- 640×360、60 FPS、10 秒静态 ASS：Rust smoke test 通过，确认 VAAPI decode、event-VFR cache overlay、H.264 VAAPI encode；
- 同一素材改为 `\fad` 动态 ASS：Rust smoke test 通过，确认不走 event cache，使用 60 FPS live band overlay；
- 640×360、30 FPS、5 秒 10-bit 输入：Rust smoke test 通过，确认 P010 decode/upload、AV1 VAAPI encode，输出 `yuv420p10le`。

## 同机 A/B 与并发验收

A380 / renderD128，8 秒 testsrc2 H.264 60 FPS 输入，每段 480 帧，两段静态 ASS 对白；AV1 VAAPI CQP 24、preset 4。以下为单次端到端测量（含探测、缓存生成、编码，不含编译及输入生成）：

| 素材 | Python | Rust | Python/Rust 解码帧哈希一致 |
| --- | ---: | ---: | ---: |
| 1920×1080 60 FPS | 3.299 s | 2.654 s | 480/480 |
| 3840×2160 60 FPS | 6.637 s | 6.022 s | 480/480 |

两端均实际使用 event-vfr、5 个透明状态，输出时长 8 s、帧率 60 FPS、8-bit AV1。两端输出文件大小亦一致。Rust 并发渲染两个同样的 4K60 任务共 8.920 s，两份输出分别为 480 帧 / 8 s，均使用 VAAPI decode + event-vfr，未发生渲染降级。首次并发测试因 40 秒命令总超时（其中重新编译约 33 秒）未完成，重新以充足超时测试通过；未发现残留 FFmpeg 进程。

以上仅代表隔离合成素材单次测量，不代表全部实际视频吞吐；尚未测量长视频、真实投稿样本、GPU busy 或显存峰值，也未证明远程 RenderWorker 性能等价。

原 profile 的 GPU 开关默认保持有效；如确需全局自动选用已配置设备，可显式设置 `VIDEOROLL_V2_RENDER_GPU_AUTO=true`。

## 可复现命令

先生成隔离素材：10 秒 `testsrc2` 60 FPS H.264 + 两段 ASS 静态对白；另生成 5 秒 `testsrc2` 30 FPS `yuv420p10le` 素材。将素材路径传给 gated Rust smoke tests：

```bash
cd rust
DATABASE_URL=postgresql://127.0.0.1:5432/unused \
VIDEOROLL_V2_STORAGE_ROOT=/tmp \
VIDEOROLL_V2_INTERNAL_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
VIDEOROLL_V2_FFMPEG=/usr/bin/ffmpeg \
VIDEOROLL_V2_FFPROBE=/usr/bin/ffprobe \
INTEL_GPU_RENDER_DEVICE=/dev/dri/renderD128 \
VIDEOROLL_V2_RENDER_GPU_AUTO=true \
VIDEOROLL_RUN_VAAPI_SMOKE=1 \
VIDEOROLL_RENDER_SMOKE_SOURCE=/tmp/videoroll-render-smoke/source.mp4 \
VIDEOROLL_RENDER_SMOKE_ASS=/tmp/videoroll-render-smoke/subtitle.ass \
cargo test --lib media::ffmpeg::tests::vaapi_event_cache_hardware_smoke -- --nocapture

DATABASE_URL=postgresql://127.0.0.1:5432/unused \
VIDEOROLL_V2_STORAGE_ROOT=/tmp \
VIDEOROLL_V2_INTERNAL_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
VIDEOROLL_V2_FFMPEG=/usr/bin/ffmpeg \
VIDEOROLL_V2_FFPROBE=/usr/bin/ffprobe \
INTEL_GPU_RENDER_DEVICE=/dev/dri/renderD128 \
VIDEOROLL_V2_RENDER_GPU_AUTO=true \
VIDEOROLL_RUN_VAAPI_SMOKE=1 \
VIDEOROLL_RENDER_SMOKE_10BIT_SOURCE=/tmp/videoroll-render-smoke/source-10bit.mp4 \
VIDEOROLL_RENDER_SMOKE_ASS=/tmp/videoroll-render-smoke/subtitle.ass \
cargo test --lib media::ffmpeg::tests::vaapi_p010_av1_smoke -- --nocapture
```

没有可用 render node 时，以上 gated tests 不代表 GPU 验收；纯规划/参数单测仍可在 CPU-only 环境运行。

### A/B benchmark

创建独立测试目录，准备 `1080p60.mp4/.ass` 和 `4k60.mp4/.ass`（1920×1080 / 3840×2160，60 FPS，8 s，ASS PlayRes 与输入一致；静态对白 1–3 s、5–7 s）。使用前述隔离环境变量，设置 `VIDEOROLL_RENDER_BENCH_ROOT` 为该目录，分别执行：

```bash
cargo test --lib media::ffmpeg::tests::vaapi_render_benchmark -- --nocapture
cargo test --lib media::ffmpeg::tests::vaapi_two_concurrent_render_benchmark -- --nocapture
```

Python 对照使用 `render_burn_in(..., video_codec="av1", use_intel_gpu=True, intel_gpu_render_device="/dev/dri/renderD128", preset="4", crf=24, render_metrics=metrics)`。使用 ffprobe 核实帧数、时长、分辨率、像素格式；使用 FFmpeg `-map 0:v:0 -an -f framemd5 -` 对比解码帧哈希，而非 MP4 容器整体哈希。
