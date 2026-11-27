# Rust YouDub 音频交接与远程 P2 渲染

对照 `src/videoroll/apps/dubbing/youdub_native.py` 与 `youdub_job.py`：原 Python 在新 YouDub 任务创建前设置 audio_only.flag，从上游取得混音、纯人声、对齐 SRT，创建持久渲染任务；只有渲染完成后才认可 P2。

## 启用与兼容边界

- 默认保持旧的全视频 YouDub 运行模式；设置 `VIDEOROLL_V2_REMOTE_RENDER=true` 与 `VIDEOROLL_V2_YOUDUB_AUDIO_ONLY=1` 后，新建 YouDub 任务启用音频-only。已有模型任务不重写标记。
- 更新远程 RenderWorker 至包含 `dub_burn_in` 能力的版本（worker 自报 0.1.2）。旧节点继续 P1，不能领取新的 P2。无兼容节点时 P2 安全等待而不是假成功或在模型机自动编码。
- 仍调用原 YouDub VoxCPM2/Demucs 第三方模型。VideoRoll 的调度、租约、资产、渲染、回执均由 Rust 控制，不新增业务表。

## 状态与资源

1. Rust 冻结源视频及翻译 SRT 指纹；桥接器从实际 session metadata 判断 audio_only，返回混音、纯人声和对齐字幕的路径。校验运行目录边界、音频流、字幕内容及输入指纹。
2. 生成独立工作流资产与哈希，预先冻结 render_spec（输入/混音/ASS/SRT、渲染配置、模型任务 UUID）。同一 operation_inbox ID 对应唯一 render_executions ID；重复交接只接受相同证据。
3. Dubbing 阶段进入 awaiting，原 API 显示 rendering 及 96–99% 进度。只有宣告能力的节点能 claim，暂停/继续/取消及自动重试沿用渲染租约和尝试上限。
4. 节点先 stream-copy 视频并替换音轨，补齐短音频至视频长度，再复用原 GPU/CPU ASS burn-in 管线。字幕独立于 P1，不覆盖 P1 SRT；视频成品登记为 video_dubbed，不是 video_final。
5. 验证媒体流、SHA256、租约、任务/工作流/阶段身份后持久化输出；恢复扫描可补结算丢失的回调。只有同一渲染的已完成回执能原子写入 dubbing proof 与下一阶段。

## 限制

- 本地 Linux 编译及无 GUI 的 worker 模块验收不能替代 Windows、D3D11/QSV、真实 VoxCPM2、真实平台验收。尚未构建 Windows 安装包或部署生产。
- 音频-only 开关是本批主动选择的保守兼容策略，未自动改变现有生产配置。开启前应升级至少一个远程节点。
- 现有 ASS 默认字号/样式生成与原 Python 样式的全部差异仍待验收；本批没有迁移所有样式选项。超过自动渲染重试上限后，原手动配音 retry 接口仍可能创建新工作流并重新合成；后续应保留已完成音频，仅重排渲染。生产真实数据未写入。

## 本轮验收（2026-11-27）

- 最终 Rust 库测试 250/250；新增真实隔离 PostgreSQL 专项独立通过：mock YouDub 生成真实音频，重复交接，旧节点拒领、能力节点领单、HTTP 传输音频与 ASS、上传 MP4、伪造完成拒绝、暂停/恢复及丢失回调恢复，最终仅一个投稿阶段，不覆盖 P1。模型合成使用测试替身，未加载真实 VoxCPM2。
- Linux headless worker 12/12，实际 FFmpeg 音轨替换测试原始 50/50 帧哈希相同、440Hz 源音轨换成 880Hz、0.7 秒音频补齐后视频仍完整 2 秒；原 QSV 缓存 CPU 像素测试 480/480。
- 最新 videoroll-v2 debug 服务构建通过；使用原生产 schema-only DDL 的六组渲染事件/取消/回执恢复断言全部通过，无生产写入。
- 日志：/tmp/vr-b7-final-lib2.log、/tmp/vr-b7-final-handoff.log、/tmp/vr-b7-worker3.log、/tmp/vr-b7-bin.log、/tmp/vr-b7-render-events.log。
