# VideoRoll Rust v2 功能差异

## 2026-11-28：迁移验收失败（FAILED）

用户已明确判定本轮 Rust 迁移失败。现有实现仅作为重写参考，不作为原版兼容基准；此前接口覆盖率、估算完成度及模拟测试通过均不能代表迁移成功。当前已发现 Cookies 持久化方式偏离原数据库契约、手动下载入队后错误返回 409、旧失败状态残留、下载参数/元数据行为差异及额外 V2 对外接口。真实 YouTube 自动下载尚未通过。保留现有提交和未提交修复，不回滚或删除生产数据，不宣称完成切换。

重写规则：先阅读原 Python 源码、调用链、数据库模型、配置默认值、原测试及前端调用，再检查 Rust 对应实现，逐项迁移原行为。保持原公开 API、字段、HTTP 状态、任务状态、原 PostgreSQL 业务表与媒体引用；不新增业务表或对外接口，不擅自优化或改变业务语义。第三方 yt-dlp、SAU、YouDub、ffplayout 等保持原集成边界。对照测试以原 Python 行为为准，使用隔离数据库与实际 HTTP 调用，区分模拟测试和真实平台验收。每轮记录源文件、函数、差异、修改、测试结果与剩余缺陷，下一轮从未完成项继续。

重写顺序：YouTube 设置/下载/进度/恢复/自动工作流 → 任务调度及历史兼容 → ASR/翻译/词典/Embedding/RAG → 字幕渲染/远程节点/YouDub → B 站/SAU 投稿 → 播控/存储/回收 → 原前端全交互及 217 条原 API 全量行为差分。每次修改前检查工作树与其他开发任务，避免覆盖并行工作；仅提交已验证的变更（中文提交），不自动部署、不操作生产库、不真实投稿。

## 重写接力记录：第二轮（2026-11-28）

- 基线仍为 `ed42980`，接续未提交工作，未改生产配置/库/服务。第一轮手动下载原 HTTP 接口隔离测试证据 `/4t/vr2-youtube-rewrite-test.LQGeLj`，不是本轮重新执行结果。
- 本轮参考 `youtube_downloader.py` 的 `_extractor_args`、`_merged_extractor_args`、`build_ydl_opts`、`download_youtube_video`，`youtube_service.py` 的 `effective_youtube_settings`、`get_download_progress`，`youtube_settings_store.py` 的代理/登录态及 Cookie 格式，以及 `config.py`、原下载器/设置/字幕测试。修复显式空代理不应回退环境变量；保留原 `YOUTUBE_EXTRACTOR_ARGS_JSON` 对象配置，兼容模式替换 player_client、普通覆盖合并列表，其他提取器选项保留；视频、元数据、字幕、来源扫描/解析共用参数；补全 Cookie 登录态名称，恢复 `FFMPEG_PATH` 配置别名。未新增公开接口或业务表。
- 证据：`/4t/videoroll-rust-trial-build-ed42980/round2-check.log` 编译检查通过；`round2-unit.log` 两项新增测试通过；`round2-lib.log` 272 项通过、0 失败（普通库测试，依赖显式环境变量的外部集成分支不等于均已实跑）。原 Python unittest 下载器 7、设置 2、字幕选择 6 共 15 项通过；实际 yt-dlp CLI 参数解析与 Python `build_ydl_opts` 的兼容模式/skip 示例一致。pytest 不在现有 Python 环境，原并行下载 pytest 套件尚未执行。`git diff --check` 通过。
- 下一轮优先：严格复刻原 Cookie 规范化（域名前导点、TRUE/FALSE、空格分隔、标准头、已有字段默认值）和 `YOUTUBE_COOKIE_FILE` 优先级，核对 `FFMPEG_PATH` 实际传给 yt-dlp、旧 proxy 支持范围；对照 Python aria2c 大文件选择/重试回退与进度，补原 log 资产及封面/元数据字段回退、旧任务进度读取及自动模式恢复。修复前先读对应 Python 函数及原测试。完整 HTTP 回归及真实 YouTube 测试仍未完成，不可将本轮编译或单元测试计为生产等价验收。当前工作树包含前轮混合未提交修改，暂不将其整体提交；完成相关回归后分组审查提交。

## 重写接力记录：11-28 上午人工接手

- 夜间调度结束后只有 Cookies 规范化代码和 3 项专项测试新增，未形成提交。接手保留全部修改，继续对照原 `youtube_service.effective_youtube_settings/get_download_progress`、`youtube_settings_store`、`routers/settings.py`、`download_dispatch.enqueue_download/download_operation_status`、`routers/youtube.py` 和 `video_pipeline._wait_for_youtube_download`。恢复环境 `YOUTUBE_COOKIE_FILE` 优先级（缺失且启用 DB 时回退）、原字段状态报告、域名归并；下载/字幕/元数据/来源共用原文件选择；历史进度读取原 `app_settings` 和 `operation_inbox`，过期租约映射 queued，完成映射 100%，不更改历史账本；原异步下载入队不隐式开始字幕/投稿，失败任务明确拒绝，异步 4 次、同步 1 次。原公开 API 未增加，原业务表未增加。
- 验证：`/4t/videoroll-rust-trial-build-ed42980/round-next-lib.log` 273 库测试通过（历史进度/异步变更前）；`round-next-http-build.log` 最终主程序编译通过；`/4t/vr2-original-contract.vrMkpx/legacy-actions.log` 原 V1 HTTP 下载、入队、历史进度、原字幕和 P2 入口共六组断言通过，独立 PG 及合成 FFmpeg 媒体；此轮附加 Cookie 测试脚本 SQL 百分号转义失败，独立重跑 `/4t/vr2-cookie-contract.z2v5iE/cookie-contract.log` 两组通过，验证环境文件优先、DB 回退、字段、清空及无投稿。测试仅使用合成 Cookie 和模拟 yt-dlp，不代表真实 YouTube 或整个自动流水线验收。未触碰生产环境。
- 后续 P0：原任务列表/详情、资产、成品、流媒体仍错误过滤历史 Python 所有权或 `v2/` 路径，先恢复原只读查询与实际 Range 验收；写操作保持独立所有权检查。随后完成原进度写入/下载诊断日志资产、封面、真实 aria2c 选择与回退、原 proxy 兼容和 FFmpeg 参数、自动恢复及其余模块。未通过全量行为差分前继续 FAILED。前端候选部署改动不与本次后端提交混合。

## 重写接力记录：历史任务及媒体读取

- 基于 `0d0c44c`，对照原 Python task_service 的任务列表/详情、标题回退、成品列表、上传进度及 asset_service 资产查询/下载。V1 只读接口恢复全部历史任务及原存储键；任务标题按原元数据回退，历史 B 站上传进度从原 publish_jobs 读取。资产列表恢复时间正序，成品列表恢复原查询范围和封面关联，媒体读取允许原路径且验证任务与资产归属。写操作仍执行独立所有权检查；只读不会接管历史任务或生成新作业。
- 最新主程序编译成功；独立 PostgreSQL、真实 HTTP、合成 FFmpeg 视频、模拟 yt-dlp 回归七组通过，日志 `/4t/vr2-historical-read.NtGVxt/legacy-actions.log`。新增测试使用 Python 所有权的旧任务、原 raw 存储键、旧标题及封面、HTTP Range 206 与跨任务 404，检查所有权不变且未产生 Rust 作业。未操作生产。该测试不是实际 YouTube 或完整原版端到端验收。
- 后续优先补齐历史字幕/配音/投稿记录读取、资产 DTO 与流媒体响应细节；继续对照下载日志/封面、真实 aria2c/代理参数及工作流恢复。全量差分及真实平台验收仍未完成，继续 FAILED。

## P0 已修复：真实 YouTube 下载验收遗留（2026-11-28）

基线 `8e94992`。使用昨晚原任务 `aa1c8626-fe2a-4f9f-8ab4-908697427f88`（NASA | Evolution of the Moon，UIKmSQqp8wY）及现有数据库 Cookies，在隔离 PostgreSQL 和独立媒体目录调用原同步下载接口，真实 yt-dlp 下载成功，HTTP 200、52,477,014 bytes、1 次尝试、任务 downloaded、旧错误清除、metadata_json 134,502 bytes、0 条投稿。测试目录 `/4t/vr2-nasa-real-replay.XlDMCc`。生产原任务未修改；不代表完整自动模式通过。首次准备隔离数据库时迁移顺序错误，修正后重放通过。

- [x] **P0 封面下载及登记遗漏。** 实测 `cover_asset` 缺失。直接移植原 `src/videoroll/apps/orchestrator_api/youtube_downloader.py:328-467` 的 `pick_thumbnail_url`、`_stream_thumbnail`、`download_thumbnail_jpg`，以及 `services/youtube_service.py:789-826` 的封面登记流程：按原规则选择缩略图、使用原下载配置、转换 JPEG、SHA256 去重、原 `final/{task_id}/cover_youtube` 命名规则及 `assets.kind=cover_image`，下载失败回退已有封面且不影响视频成功；原事务和清理行为保持一致。验收：真实 NASA 下载返回非空 cover_asset，数据库资产与 JPEG 文件一致，重复执行不重复登记，原失败回退通过。
- [x] **P0 分离音视频下载进度回跳。** 实测原进度接口依次报告 downloading 57% / 31,453,932 bytes、99% / 2,498,967 bytes，字节数随音视频分段切换回退。直接移植 `services/youtube_service.py` 下载进度 reporter 的 `_requested_total`、`_download_key`、`hook`（约 622-666 行）：按分段/文件标识记录已完成字节，存在 requested_total 时聚合各分段，保留原 95% 下载阶段上限及单次尝试内进度非回退约束；无总量时保持原回退逻辑；仅真实 retrying 时重置；storing 99、completed 100。对照 `youtube_parallel_downloader.py` 原 aria2 进度字段。验收：独立音视频分段、未知总量、重试及完成场景按 Python 原 hook 逐事件差分；使用同一真实 NASA 视频复测，不以单一百分比模板替代原聚合规则。

### 本轮修复及真实复测

- `youtube_meta.rs`、`ingest.rs` 移植 Python 缩略图评分、原代理和 UA、逐跳原域名校验、30s 超时、大小上限、FFmpeg JPEG 比例、SHA256 去重、原 `final/{task}/cover_youtube` 文件命名及原 assets 登记。原 Python 实机 `download_thumbnail_jpg` 与 Rust 对同一 NASA 缩略图均输出 1146×716 MJPEG（原 1146×717 FFmpeg crop 指令因色度采样取整）；不修改原参数。封面下载/转换错误非致命，保留既有封面；新文件在事务失败或重复时清理。真实下载和重复读取已通过；故障注入（网络失败同时已有封面）及崩溃中断专项仍待补测。
- `download_runtime.rs` 移植 Python reporter 的格式 ID、已完成字节累计和 95% 上限。原 Python hook 直接执行的四事件期望 (50,150,31)、(100,150,63)、(105,150,66)、(150,150,95)，新增 Rust 对照用例使用相同期望。CLI 每流 info_dict 可能不含 requested_downloads，使用 before_dl 输出选中格式；HLS 缺少精确大小时按 bitrate×duration 估计并随传输修正，不把已知单流大小误当总量，未知总量仍保留原 fallback 并按分段推进。补 storing 99、completed 100/ETA 0、失败保留当前进度。该 CLI 适配是为恢复原累计语义，不是新增公开 API/业务表。
- 初次真实重放 `/4t/vr2-cover-progress-real.DVFTXj/replay.log` HTTP 200、封面 148367 bytes、零字节/百分比回跳，但发现部分总量错误导致提前 95%，以及测试错误假设 717 像素。运行原 Python 封面函数确认 716，继续修正 HLS 总量后复测。最终 `/4t/vr2-cover-progress-final.WmaBB6/replay.log`：HTTP 200、视频 52,477,014 bytes、metadata_json 134,815 bytes、JPEG 148,367 bytes；10 次 active 采样无字节或百分比回退，94→processing 95→storing 99→completed 100；SHA256 一致，重复请求返回同一 cover_asset ID；原库克隆的任务状态 downloaded、download succeeded 1/1、三类资产各 1、0 投稿。HLS 估算总字节可随实际片段变化，非累计下载字节回退。使用真实 yt-dlp、现有数据库 Cookies、独立 PG 和媒体根，未更改生产任务或部署。
- 两项主要缺陷已修复且真实复测通过；上述未覆盖异常专项继续记录为待验证。原下载器 7 项 unittest 通过；最终 Rust 全库测试 276 通过、0 失败（环境条件未满足而提前返回的外部集成分支不等同于实跑）。最终构建及测试日志 `/4t/videoroll-rust-trial-build-ed42980/cover-progress-final-build.log`、`/4t/videoroll-rust-trial-build-ed42980/cover-progress-final-lib.log`。整体迁移仍为 FAILED；不将本次下载验收等同于自动流水线、全部 API 或生产切换验收。

## 2026-11-28 ASR 原版行为重写

- 核查原 NASA 任务音轨 161.4335s，原 Python 120s/5s 和旧 Rust 45s/4s 的 VAD 均无语音片段，不能以此推断识别故障。修复原版 OpenVINO 250ms/30s/500ms/180ms VAD 参数、三项静音阈值、紧凑 float32 数组、时间戳回退、垃圾文本与可信度/幻觉过滤。本地引擎恢复 120s/5s PCM WAV，External Whisper 恢复完整 WAV 流式上传，Groq 45s/5s 和 Cloudflare 30s 不变；更新 ASR 检查点指纹以避免复用旧策略结果。
- 原版 subtitle_jobs 以同一 Rust 作业 UUID 写入，原请求结构及 source_segments_key/final_subtitle_segments_key 对应原 sub/<task>/ 路径、assets 登记，成功空识别写入 [] 和 no_speech_detected=true；恢复原进度、日志路径、历史列表读取，终态/失败/暂停/取消及租约同步。无需新增业务表或公开 API。日志目前为简化阶段摘要，仍须补齐原详细实时日志和失败路径；逐词/逐段 confidence 的完整保留、全长语音时间轴一致性、所有重启/断点恢复故障注入及旧任务未补写记录仍待验收。队列进度/心跳为最后一批变更，不能以早期集成测试替代其最终验收。
- 真实验证：原 Python OpenVINO 和新版桥接在同一 GPU、同一合成英语语音、同一模型上得到相同文本和时间戳（0.044–5.764s），结果 /4t/vr2-asr-real-diff.XxdFlM/{original,bridge}.json；隔离真实 OpenVINO 无语音路径通过原 subtitle_jobs/原请求 Pydantic 校验/[]/日志/列表无重复（/4t/vr2-asr-ledger.6Ei68r/ledger.log）；External Whisper 模拟 HTTP 验收完整 65s WAV，422 回退再次上传完整 WAV、原始及最终快照、原字幕任务及零投稿（/4t/vr2-external-wav.E2lIpk/external.log）。不是真实在线提供商验收。
- 原 Python 专项 pytest 64/64 通过；合并运行 98 项中 92 通过、6 失败（长音频测试涉及 NumPy 重复导入，单独运行首个用例又遇到 pytest.approx 嵌套结构不支持）。不修改原测试来掩盖问题。Rust 主程序构建和 276/276 库测试通过，随后队列/心跳补丁单独核查；日志 /4t/videoroll-rust-trial-build-ed42980/asr-parity-{build,lib,final-check}.log。真实 Faster-Whisper、Groq、Cloudflare 平台、长期断点恢复和详细日志尚未完成最终验收。继续 FAILED，未部署或改生产库。

## ASR 后续修复检查点（2026-11-28）

- 基于 `80321fd`，对照原 processing.py 的 `_segment_confidence`、词级概率均值和 `_combined_segment_confidence`，增加可选 confidence、词级 probability，贯穿识别结果、重叠合并、可读性拆分和历史快照读取；未知值不伪造。更新检查点策略指纹，避免复用缺失新字段的旧识别结果。
- 对照原 worker.py 音频提取及日志路径：完整 16kHz 单声道 PCM WAV 保存为 `work/{task_id}/audio_...wav`，登记原 audio_wav 资产；同一作业重试通过原请求关联与 SHA256/大小校验复用音频。External Whisper 直接上传此完整 WAV，其余引擎由该文件生成有界识别窗口。恢复 audio_extracted 状态。日志追加提取、窗口、重试、结果和失败摘要，使用原 log 资产及日志路径；不覆盖旧不可变日志。原完整实时事件通知、详细供应商失败日志、跨代历史音频复用及手动字幕兼容仍需验证或补齐。
- 首轮 `cargo check --tests` 因新增字段后七处测试构造缺失而失败，修正后全部测试目标编译检查通过（`/4t/videoroll-rust-trial-build-ed42980/asr-assets-final-check.log`）。完整 build/lib 测试在保存此检查点时尚未取得最终结果；真实音频资产/日志、置信度差分和故障注入未验收，不能标记完成。未部署、未修改生产库。
- 只读核实原 NASA 任务 `aa1c8626`：第三代仍 succeeded，任务 rendered，video_raw/video_final 各 52477014 字节，subtitle_jobs=0、publish_jobs=0；属于旧媒体试跑，不能作为本轮 ASR 修复通过证据。保留整体 FAILED 状态。

## 2026-11-28 NASA 第四代工作流实测

- `d9fd669` 主程序构建、全部测试目标编译及 276 项库测试通过。使用原生产任务 `aa1c8626-fe2a-4f9f-8ab4-908697427f88`，新建第四代 `53c3a4fe-4fda-4e34-87fa-3d55471947e8`，只启动专用 Worker，禁止真实投稿、配音和资源清理，不替换正式服务。前两次字幕尝试因临时 WAV 未与最终资产同目录而被原资产安装约束拒绝；等待重试时停止旧 Worker。修正为目标 `work/{task_id}` 内的私有 tempfile，保持原原子安装校验，构建通过后使用同代第三次尝试恢复。
- 11:07:46 第四代工作流 succeeded，task rendered，无任务错误；原 subtitle_jobs `8e9e2d4d-68bb-451f-bdae-ac52276ed911` succeeded/100，错误及租约清空。完整 WAV 5,165,950 bytes，16kHz mono PCM s16le，161.4335s，原路径与 SHA256 校验一致；原始识别 JSON 为 []，2 bytes，no_speech_detected=true。实际日志覆盖两次失败及后续音频提取、两个 120s/5s overlap 窗口、无语音结论。原视频复用，翻译/硬字幕编码没有实际执行；0 投稿。内部成功作业 last_error 仍残留前次路径错误，待修复，不等于当前任务失败。此次路径修复的完整库回归尚未重跑，实际主程序构建与生产单任务回归已通过。
- 独立原 Python OpenVINO 对同一完整 161.4335s 音轨测试：VAD 开启为 []；关闭 VAD 得到 0–2.06s 'you' 和 90–144.98s 'I'm sorry. Thank you.'，另有 3 条幻觉或无效时间字幕被原算法丢弃。不能据此确认有可信旁白，未人工试听。证据 `/4t/vr2-nasa-full-audio.qE2EN0/results.log`；工作流日志目录 `/4t/vr2-nasa-d9fd669.SZzGwq`、`/4t/vr2-nasa-audio-retry.E5rNAN`；构建 `/4t/videoroll-rust-trial-build-ed42980/asr-audio-colocated-build.log`。正式 API 服务未替换，整体 FAILED 不变。

## 2026-11-28 原渲染策略复刻及 AV1 自动码率

- 对照原 `processing.py:render_burn_in`、`_prepare_ass_event_cache`、`_write_ass_event_timeline` 及提交 `9223428`、`ebd43d1`、`d50b781`、`af129c3`、`1d649a7`：保持 VAAPI 硬件解码、8/10-bit、裁剪字幕图层、静态缓存、event-VFR、动态字幕及兼容回退；恢复硬字幕输出音频 stream copy、AV1 缺省固定质量 24、软件 AV1 缺省 preset 4，复用统一 VAAPI 编码参数。MP4 faststart 保留。明确指定质量仍为 CQP/CRF；用户新增授权的 Intel AV1 自动质量按可信视频流码率取 1.5 倍与像素率下限的较大值（0.016 bit/pixel/frame，250kbps–120Mbps，向上取整到 kbps），VBR maxrate 2×、bufsize 4×；缺失/无效码率不猜容器总量，恢复 CQP24；VBR失败优先同GPU CQP再原软件回退。未对其他编码器/远程 RenderWorker 引入自适应策略。此为新优化，不冒充 Python 原功能。
- 原 Python 同一实际黑洞源/ASS 测试 `/4t/vr2-encode-vbr-study.Fg5CvT/results.log`：CQP24 + copy 67,981,666 bytes / 8.623s；CQP24 + AAC/faststart 68,690,483 bytes / 8.36s（单次对照不支持音频转码是两倍耗时的主因）；VBR 1Mbps 14,810,301 bytes / 8.85s，完整2793帧相对CQP24 SSIM 0.990172；QVBR失败，未采用。旧 Rust 独立 benchmark `/4t/vr2-old-rust-render-bench.G3ytPy/result.log` 同样93s源重复两次，固定preset4，7.37/7.46s；并非真的1080p60/4k60两个不同样本，不能与前生产任务15.67s直接等同。调试sha2对约65MiB文件单独计算3.621s；未定位历史阶段额外耗时的全部来源。新增探测、预检、编码、输出probe、fsync、asset_install和总阶段耗时指标，保留原资产安全校验。
- 新 Rust 实测 `/4t/vr2-rust-av1-adaptive.oeauVl/benchmark.log`：固定CQP24/未指定preset 输出67,981,666 bytes，7.300s，实际视频码率5,694,676bps；自动VBR目标995,000bps，实际1,125,464bps，输出14,754,296 bytes，7.718s；两者均AV1 VAAPI event-VFR、32缓存状态、1080p/29.97fps/93.221s、Opus 134,538bps。原片和两种输出音频压缩流SHA256均为6e35698e3008afecec08283ef5026f42faeb712b6dc193385f65155a596b1d9e。固定质量与Python结果码率、体积一致，整容器哈希因封装不同不一致；真实画质对照为前述Python VBR实验，非新Rust成品独立SSIM。Rust lib 278/278通过（条件门控外部测试除外），日志 `/4t/videoroll-rust-trial-build-ed42980/render-adaptive-lib.log`；实际GPU专项1/1通过。未运行最终主程序构建/生产工作流重放、未部署、未改生产数据库；VBR失败回退、复杂字幕/10-bit自动码率和其他素材视觉验收仍需专项。整体迁移状态FAILED不变。

以下为历史审计记录，不再视为当前验收结论。当前不满足完整生产替换门槛。

2026-11-23 迁移状态（最新接口审计）：**175/217** 条原版 API 具有部分 Rust 原生适配，剩余 **42** 条尚无原生 V1 入口；**0** 条达到完整生产等价签收。测试采用一次性数据库、回环侧服务和原版 Python 同库差分；生产 Python 服务不切换。

| 系统 | 隔离验收已覆盖 | 仍需完成 |
|---|---|---|
| 任务/调度 | Postgres 事务、租约、去重、Unknown 待核实；旧版 TaskRead、新建本地/YouTube、停止恢复、隔离工作流（仅 V2 自有任务） | 所有旧 Hatchet 任务迁移、完整 217 API、跨机器重启与并发压测 |
| 影音下载 | YouTube 来源扫描、单视频、现有文件续用 | 真实 yt-dlp/aria2 代理、并发和断点续传 |
| 字幕 | Whisper 兼容 API、真实 FFmpeg SRT/ASS 硬字幕 | 全部 ASR Provider、长视频与原版特殊参数 |
| 翻译 | Index-Translate 模拟请求、缺行补译、Provider 选择 | 正式 API 配额、术语与长上下文质量 |
| 词典到知识库 | 原版词典多格式批量导入、来源/条目管理、提升及在线向量生成；原版知识项列表、实际新增/去重/删除、OpenAI 兼容 Embedding HTTP 向量生成和重建 | 本地 SentenceTransformers/OpenVINO 执行、知识批量导入、完整 Agent 消费与超大批次/断电验证 |
| Embedding 管理 | 原版设置驱动的模型目录、HF 下载、pgvector/HNSW 诊断、原生 HTTP 在线向量生成及维度校验 | Python local/openvino 实际推理、生产 Embedding 服务、向量索引性能和 Agent 消费链路 |
| RAG Agent | 有限工具调用、知识检索和证据入库 | 多代理、字典、全部网络研究、原版语义召回 |
| YouDub P2 | 独立模拟 bridge、确定性 ID、P2 视频绑定 | 真正 VoxCPM2/Demucs GPU 和配音对齐 |
| 渲染 | 本地真实 FFmpeg、远程 Worker 协议模拟 | Windows/Linux 真 GPU、长时间稳定性 |
| B 站投稿与字幕 | 原生 UPOS、Unknown 投稿保护；原生 SRT 独立同步、双 P CID 验收、疑似已提交时阻止自动重试、审核后恢复 | 真账号双 P 投稿、活动、字幕草稿与合集真实验收 |
| 其他平台 | Rust 原生 SAU 外部服务编排：账号检验、持久投稿意图、单平台与三平台批量入口、模拟 SAU 回执、超时防重 | 真实 Douyin/小红书/快手账号与浏览器发布，跨平台一致性、完整可回滚批次 |
| V1 投稿批次与封面 | 原版 publish_batches + publish_jobs 真正读取并合并最新目标结果；V2 自有任务的真实图片封面流式上传、独立图片解码与尺寸校验、规范编码后登记资产引用；退役 YouDub 端点明确 410 | 真实平台的批次创建、真实封面投稿验收、旧任务所有权移交 |
| 播控 | 中文文件名导入、引用保护、解除后回收 | ffplayout 真正节目单编辑与播放控制 |
| 存储/媒体 | 引用式 GC、播控资产保护、V1 合同形状的成品/资产列表、真实 Range 流媒体、FFprobe 校验并事务提交的本地 multipart 上传 | 大规模压力、复杂原版上传选项、旧资源迁移 |
| 前端 | Rust V2 独立页、原版 React 全页面隔离预览 URL 开关、原生 V1 读写适配；开发台可看到 API 差异清单 | 193 个旧 API 前端引用逐个移植、完整页面交互/状态/E2E |

原版 orchestrator 有 132 个路由装饰器，Rust 当前静态代码审计发现约 104 处路由声明（不同 API 命名空间及动态注册不可直接相比）。此数量不能直接换算功能覆盖率，但提示需要逐接口对照。旧 API 只读兼容层不是 Rust 原生实现。

隔离脚本使用模拟远程服务和合成视频，不能替代真实发布和 GPU 验收。只有通过全部原版契约、故障注入、真实设备运行和可回滚部署后，才允许评估生产切换。


## B 站原生字幕独立同步

- 使用 Rust src/bili_subtitles.rs、SQLx 与 reqwest，无额外投稿 SDK。
- 仅扫描已确认、带 native_bilibili=true 的 Rust 投稿回执，不会误写旧版或其他平台。
- 独立持久化 aid、bvid、SRT SHA-256、分 P cid、提交状态；字幕失败不改变 PUBLISHED。
- 受保护的管理 API 可列出任务、查看详情、在真实核实后手工处理 unknown。
- 后台 Worker 每 45 秒扫描回执并处理；重启可以继续安全阶段，preSave 未知结果拒绝重复提交。
- 最多 8 次安全阶段重试，源字幕引用上限七天，防止磁盘长期堆积。
- 隔离测试覆盖单 P、双 P、HTTP 503、重复任务执行及人工核实后的恢复。
- B 站投稿/字幕接口属于非公开稳定 API；真实账号与 B 站服务器仍需验收。


## 2026-11-22：旧版 API 与前端迁移审计

- 源码定义 217 个 FastAPI 路由装饰器；原版 orchestrator 有 132 个。原版前端有 193 处旧 API 文本调用候选（160 种 URL 模式），动态路径可能遗漏。
- rust/scripts/audit_legacy_routes.py 生成清单，--check 检查变动。rust/docs/LEGACY_ROUTE_CATALOG.json 和 FRONTEND_API_REFERENCES.json 用于逐项复核。Rust /api/v2/parity 返回源声明数量与原生适配数量，全功能完成标志仍为 false。
- Rust /api/v1 局部兼容层仅处理 V2 自有任务。原生适配包括管理员验证、任务列表/详情、YouTube 手动/自动入库、本地视频上传、停止/继续、字幕/配音/投稿任务历史、成品/资产查询、HTTP Range 视频读取、播放链接、Linux 系统资源。
- V2 自有手动任务由 vr2_managed_tasks 表标记，不会将 V1 任务作为 V2 所有任务修改/调度。
- VITE_RUST_V2_FULL_UI=true 可构建原版 React 页面的 Rust 隔离预览；通过 VITE_RUST_V2_LEGACY_API_URL 指向独立 Rust。默认不开启。前端页面存在或构建通过不表示所有交互可用。
- 未实现的功能返回明确 404/错误，不会偷偷转发给旧 Python 写入 API。
- 隔离 Postgres+FFmpeg 验收包括任务建立、multipart 流式上传、源类型/资产归属、重试恢复、Range 读取及 YouTube 入库。
- **生产门槛未达到：真实多平台投稿、原版高级工作流和大量独立服务 API、完整前端交互仍未通过契约验收。**

## 2026-11-22：投稿元数据与 YouTube 元数据原生迁移（第二批）

- 原版 React 投稿编辑器的 GET/PUT /api/v1/tasks/{task_id}/publish_meta、GET/POST .../publish_meta/draft 已改由 Rust 原生实现，没有 Python 兼容代理。
- Postgres vr2_publish_meta 保存投稿信息、SHA-256 与修订版本；仅 V2 所有任务可编辑，投稿作业产生后及结果未知时禁止改稿。
- 原生 B 站提交读取已保存的标题、分区、标签、转载来源、话题、活动及支持的高级 Web 字段。额外字段白名单以外明确拒绝，不允许悄悄忽略。
- topic_id、mission_id 只做正整数结构校验，尚未接入 B 站活动报名资格核实，不能认定为已认证活动。
- 草稿模式 default/source/auto 已接入。源元数据来自真实 yt-dlp 元数据缓存。不自动杜撰分区，也不假装完成 AI 标题翻译或 AI 分类。
- 原生 GET /api/v1/tasks/{task_id}/youtube_meta、POST .../actions/youtube_meta 使用维护中的 yt-dlp 单视频 JSON 提取能力（独立进程、输出上限、ID 校验及超时）。原始 JSON、校验和及任务标题记录进入 V2 独立资产存储。
- GET .../youtube_download_progress 读取真实 V2 下载作业状态和已登记文件大小。正在下载阶段尚未提供细粒度字节数、速度和 ETA；不能把这种兼容接口等同于实时下载进度已全面迁移。
- 原版 YouTube proxy、cookies、aria2c 并发配置和实时进度、完整 AI 审稿、其他高级页面功能仍未迁移。审计新增 7 个原生兼容入口（总计 31），不代表 31 个完整功能均达到等价。

## 2026-11-22：YouTube 原生下载控制 / AI 审核 / 活动 ID 防误报（第三批）

- 原版 React YouTube 页面可直接访问 Rust 原生的 GET/PUT /api/v1/settings/youtube 及 POST /api/v1/settings/youtube/test。支持 HTTP(S)/SOCKS5 代理（不接受嵌入账号密码）、Cookies.txt Netscape 导入、1–16 连接并发和兼容客户端 player_client。Rust 直接使用原版 app_settings；尚未部署生产；不自动解密原版 Fernet Cookies。Cookies 在私有目录以 0600 权限保存，提取器持有独立临时副本。
- 下载阶段真正读取上述配置，使用 yt-dlp 子进程续传；内置原生 HLS/DASH 并发。只有确认 yt-dlp 至少为 2026.06.09 且 aria2c 可用时才启用 aria2c 的普通 HTTP(S) 下载器；SOCKS5 代理下不会强行启用 aria2c。没有安全版本校验则退回 yt-dlp 原生分片路径。HTTP 与 HLS/DASH 不得混用下载器。
- 通过 stdout 受限前缀和长度解析实时已下载字节数、估计大小、速度、ETA、百分比。vr2_download_progress 只存数字和作业 ID，并用当前任务租约、有效期和 workflow state 限定写入，阻止旧执行覆盖新任务的进度。原版 GET /api/v1/tasks/{id}/youtube_download_progress 会丢弃已失效执行的速度/ETA，不再虚报。
- 原生 GET/PUT /api/v1/settings/review 和 GET/POST /api/v1/tasks/{id}/publish_review /actions/publish_review 提供基于真实 V2 SRT 文本的违禁词/AI 审核。AI 模型使用现有翻译模型 Provider/ID 与 OpenAI 兼容 JSON 接口；无模型、模型报错、模型未返回明确布尔结论都会判定为不通过，绝不猜测为通过。审核指纹绑定当前稿件、审核规则和 SRT，发生变化后旧结论失效。真正 UPOS 投稿前调用审核门槛；禁用审核仅限显式设置。
- B 站原生上传在存在 topic_id / mission_id 时，从已登录创作中心固定 HTTPS 只读查询精确核对话题/投稿任务 ID。公开活动列表 ID 不会被当作真正的 mission_id；活动规则要求有实质内容。转载视频只有规则明确允许转载且没有报名、白名单、原创等未证实限制时才通过；查询失败、不唯一或不匹配时阻断上传。可选 metadata.activity_name 作为检索关键字，此本地辅助字段不会转发给 B 站投稿接口。该检查不能证明账号奖励或报名资格，不能说成已完成活动资格认证。
- React Rust 全页面预览下 YouTube 页面隐藏尚未迁移的「首页推荐定时扫描」，并明确说明 HTTP(S) aria2c 安全限制和未迁移的大文件下载阈值，而不是展示可点但无效的设置。
- 未覆盖部分：YouTube 首页推荐扫描、Cookies 加密迁移、下载限速/128 MiB 阈值、B 站活动账号级报名状态、AI 自动活动筛选、多平台 SAU 投稿、手动字幕与 YouDub 全量控制、其余旧版 API 和页面。老生产服务继续运行，新 Rust 只隔离验证。API 候选迁移计数约 38/217，且每个入口的业务等价还需逐项测试。

## 2026-11-22：Python 对照迁移第四批：任务详情页手动工作流及审计矩阵

此批为部分兼容，不是 217 个 Python API 的完整迁移。单个 V1 原生入口仅在功能、状态与安全约束等价时才能标为完成。

- rust/src/scheduler.rs 修正 V2 workflow start 的任务所有权：必须关联 vr2_managed_tasks。独立工作流分代可从 download/subtitle/render/dubbing/publish 精确阶段启动，沿用之前设置的浅层结构及字幕、渲染配置内层合并，不会为了重做字幕重新下载原片。
- 重启存在强制外部副作用栅栏：此前 pending/running/awaiting/unknown 或曾真实成功的 publish 阻止重启；明确标记 skipped 且没有外部副作用的 publish 不被误当作真实成功。活动中同阶段的重复操作只有设置一致才复用原作业；变更设置返回冲突。
- 新增旧任务详情页 V1 原生 POST actions/subtitle、actions/subtitle_resume、actions/publish 和 tasks/actions/resume_failed_recent。仅支持 Rust 已实现的 ASR/翻译和 FFmpeg 字段；无法原生执行的来源选择、引擎、特定字幕、自动发布策略和发布附加选项明确报错，绝不接受后偷偷忽略。手动投稿只支持保存过审核元数据、启用 native Bilibili 的安全部分；禁止强制重试和跳过 AI 审核。
- 新增当前 Python YouDub VoxCPM2 路由 actions/dubbing、dubbing_jobs/{job_id}/retry、dubbing_jobs/{job_id}/control。只接受成功的同任务最新字幕作业与最新原片；operation_key 不可复用在不同请求数据，原始已完成请求可幂等取回。重新尝试只能创建新的持久代际。请注意，此控制目前仅管理 Rust workflow 状态与租约，不保证正在 GPU 上执行的第三方推理进程立即停止；原版已退役的逐段改写、预览和确认不予伪装复活。
- 原生 V1 GET /settings/publish/platforms、PUT /settings/publish/platforms/{platform} 已基于原版 app_settings 提供 B 站开关。由于抖音、小红书、快手 SAU 账号和投稿尚未完成，主动启用它们会返回 409；管理页不能误显示可用。这不算 SAU 功能迁移完成。
- 修复跨代际 P2 关联逻辑：手动字幕、手动 YouDub、手动 B 站投稿可以分别属于不同工作流。只能在成功的 vr2_dubbing_runs 证明原视频、SRT 与 P2 输出都匹配、同任务归属并且 YouDub 作业成功时使用，不能因为 workflow_id 不同而误拒绝，也不能拿旧配音视频冒充当前配音。
- 增加 rust/scripts/audit_contract_matrix.py 与 rust/docs/CONTRACT_MATRIX.json、CONTRACT_MATRIX.md：对 217 个原始 Python 路由逐项记录来源方法、V1 原生映射与全面验收状态。目前所有路由的 fully_verified 均为 false。审计统计仅表示候选入口数量，而非实现率。
- 隔离验收新增真实 PostgreSQL 阶段代际与任务归属、同阶段重入、失败批量恢复、未知稿件阻断、平台开关 V2 隔离、真实执行的模拟 YouDub 手动创建及成功资产关联。原生 YouDub 与最终 B 站 P2 仍需真实设备和平台验收。

目前仍未完成：SAU 多平台账号及登录/投稿、B 站原版全部设置与追加 P2、ASR 引擎全覆盖、字幕高级控制、完整 YouDub 控制到第三方 GPU 运行时、渲染管理页面、告警与实时 WebSocket、旧版全部前端操作，以及故障注入、实际 YouTube/真实 B 站生产级验收。生产 main 与 VideoRoll 服务不变。

## 2026-11-22：任务自动恢复、SAU 管理、原生 WebSocket 与 V2 告警

- Rust 原生旧任务接口 actions/auto_subtitle_handoff：有成品返回完成，活动中的字幕/渲染任务返回既有 job；已有 SRT 从渲染续跑，否则从字幕续跑。没有原片、已停止或投稿结果未核实时不重启。旧 Python 的无语音特别路径仍需对照。
- Rust 原生旧任务接口 actions/auto_publish：读取自动投稿设置；没有启用或没有选择平台明确跳过；只有 B 站原生安全工作流可以创建独立代际。存在尚未迁移的抖音、小红书、快手目标时明确返回冲突，不丢弃用户目标或假装发布成功。
- SAU 的账号查询、登录会话创建/查询/取消、账号文件导入、状态复核、账号删除已有 Rust 入口。浏览器自动化仍由原独立 SAU 服务执行。通过显式配置的 HTTPS 或 loopback 端点和共享 HMAC 服务令牌调用，不回显 Cookie。社交平台投稿尚未实现，启用开关仍拒绝请求。
- 新增原生 WebSocket V1/V2 入口，支持同源 Cookie 验证、订阅、20 秒心跳、资源采样、V2 任务事件和 resync_required。源为 V2 event ledger，不宣称覆盖旧版所有事件与重连历史。
- 独立的 vr2_alerts 表实现旧管理页告警列表、上报、去重、确认、解决和 V2 失败/未知作业扫描。不触及 Python alert_events，扫描按最新代际判定、过载时不会误关闭遗漏的老告警。旧历史及自动告警分类尚未完全迁移。
- 隔离测试通过真实 PostgreSQL、Node WebSocket 客户端、Mock SAU HMAC 与账户导入、告警去重和状态流转。并非真实 B 站或真实社交平台验收。217 条 Python 接口中已有 62 条登记为部分原生适配；业务完全等价的签收仍为 0 条。
- 待迁移 P0：发布后的 P2 追加、SAU 真实投稿、完整字幕操作与历史任务。待迁移 P1：全部设置管理、其他独立服务接口、逐接口验收和真实设备/故障注入。生产 Python main 不变。

## 2026-11-22：Rust P0 投稿补齐（第五批，隔离）

本批仅部署在 `rewrite/rust-v2`，不改变原版 Python 生产服务。

- 新增原生 POST `/api/v1/tasks/{id}/publish_jobs/{publish_job_id}/append_p2`：要求此前 V2 独立成功投稿且只有 1 个分 P、平台 B 站已启用、返回 aid/BVID、同任务当前源视频与 SRT、成功 YouDub 配音记录、已注册并存活的配音成品。符合原版标题规则，P2 可在发布 P1 后补入，不再重建原稿。
- 原生 B 站 `x/vupre/web/archive/view` 只读获取作者原稿，在 `x/vu/web/edit` 保留原有稿件元数据、话题/活动、P1 CID 与标题；须在不可逆 edit **之前**持久化 `submitting`。崩溃/503 后为 `unknown`，禁止自动重试。人工核实接口需记录证据；已发布的 P1 状态不受 P2 失败影响。使用本地 HTTP mock 模式时禁止误访问正式 UPOS 上传接口。
- P2 成功后如 P1 原生 SRT 已提交或仍处于可重试准备态、同源 SRT 仍存活，则把字幕同步 `expected_parts` 扩展到 2，保留已确认 P1 的每 CID 提交记录，并恢复 P2 待同步状态。P1 字幕未知/处理中不会强制重启；P2 字幕同步失败不影响稿件已发布状态。维护循环会补偿崩溃后遗漏的扩展。
- 新增 Rust V2-owned SAU 投稿意图表：受保护的 `actions/publish` 分支支持 Douyin/Xiaohongshu/Kuaishou。配置必须显式开启 `VIDEOROLL_V2_ENABLE_SAU_PUBLISH=true`、同根的 `VIDEOROLL_V2_SAU_STORAGE_ROOT`、受限 SAU URL 和 HMAC secret。支持三个独立账号并校验登录状态、来源授权、AI 审核、视频 FFprobe、文件归属与对象引用。无需将浏览器自动化引擎或账号密钥导入 Rust 数据库。
- `SocialTick` 在 PostgreSQL 中领取唯一请求并持久化 `dispatching` 后通过原 SAU Python sidecar 触发浏览器 worker。即便 sidecar 返回 400/409/503，跨过调用边界后也按 `unknown` 处理，持有媒体引用直到核实，杜绝重复公开提交。读取原版 SAU 的持久作业记录时只做只读核验，只有已证明的外部 ID/URL 才计为成功。
- 原版任务详情的 `/publish_jobs` 列表合并 Rust SAU 投稿进度。另有 GET `/social_publish_jobs` 及详情；POST `/actions/publish_all` 支持 **明确指定** 1–3 个 SAU 平台、独立账号和平台元数据，逐平台返回部分接收或失败。不支持混合 B 站批次、不支持 `fresh_batch`、不伪造完成的全局 `batch_id`，统一原版批次语义仍待迁移。
- 隔离端到端验证：Docker PostgreSQL、真实 FFmpeg/V2 YouDub Mock、模拟作者 P1 信息/P2 编辑、P1→P2 投稿引用、HTTP 503 不重发、P2 增加 SRT 的并发状态/引用，以及三 SAU 账号、审核阻断、状态回执、异步后台投稿、部分成功统计。所有测试均无公开平台副作用。
- 接口审计目前为 217 条旧 Python 路由，其中 76 条只有部分原生 V1 映射，全面等价签收仍是 0 条；完整 P1 设置、WebSocket 全部消息语义、混合多平台批次、旧历史任务转移和真实平台/设备/重启验收尚未完成。

## 2026-11-22：Python YouTube 来源接口与健康检查契约（隔离）

- 已迁移旧前端所依赖的 YouTube 来源列表、新增/幂等 upsert、PATCH、删除、同步扫描、按类型/ID 扫描、202 异步扫描及持久扫描锁。直接 UC/PL ID 与 YouTube HTTPS 地址按原版规范化；@handle 仍调用第三方 yt-dlp 解析，解析失败不创建虚假来源。
- 扫描按 Postgres 租约在返回 202 前完成领取，实际调用 yt-dlp 及 V2 任务状态机。结果包含旧版 `created_task_ids` 与 `started_pipeline_job_ids`，读模型返回完整原版来源统计；`since` 使用 yt-dlp 的 timestamp/upload_date 等字段过滤（无日期则跳过），先完整枚举再从待处理项中选取 limit；长扫描通过定时 PostgreSQL 租约续期，失去所有权的 Worker 不继续提交新任务。真实长列表仍需压测。
- `/api/v1/health` 匹配 Python 的匿名 GET 200 与 `{"status":"ok"}`。生产只读字段对照发现 Rust YouTube 设置少 14 个历史统计字段，现由旧 app_settings 只读读取并返回，未启用 Rust 尚未实现的首页推荐扫描。
- SAU 浏览器自动化本身不由 Rust 重写，生产字段来自 sidecar；本地 mock 补全原版账号检查/轮换状态字段。测试覆盖临时数据库、重复创建、完整 DTO、锁竞争、异步扫描、真实 mock yt-dlp、删除与浏览器鉴权，绝不连接生产 DB。
- 本阶段完成时 V1 已有 **71/217 个部分映射**；此为历史阶段记录，当前总数见文档末尾的最新验收矩阵。

## 2026-11-22：Python 存储保留与 AI 费用管理（隔离）

- Rust V1 新增原版 `GET/PUT /settings/storage`、`GET/PUT /operations/ai-usage/pricing`、`GET /operations/ai-usage`，总计新增五个原生入口；计价采用原版 `ai_usage_events` 字段、stored micro-USD 优先、未存价请求按 (provider,model) 或 model 的每百万 Token 费率估算。
- 操作设置直接读写原 Python `app_settings`，由两套程序共享同一份记录，不影响正在工作的 Python 发布服务。存储 TTL 已衔接 V2 已确认投稿的 Cleanup 阶段：`asset_ttl_days=0` 默认不自动清理已发布视频，非零时安排对应天数后释放；引用、未知投稿和播放列表资源仍受现有保护。现已额外加入针对 V2-own 且已确认发布的历史任务的保守周期补扫；**Python 原有 V1 文件夹孤儿扫描/历史文件清理仍未迁移**，因此不是全等价。
- 全部 AI 事件统计使用真实表的 SQL 聚合，带时间窗口、model 排序、错误状态分组、最近请求、合并计价；目前 Rust 原生字幕翻译、RAG Agent 和投稿 AI 审核已把成功/失败 HTTP 尝试写入 `ai_usage_events`（含请求模型、状态码、毫秒耗时、Token、任务归属及价格，且入库失败不阻断业务）；Whisper ASR 与 Index-Translate 已进一步加入逐 HTTP 尝试记账；Embedding 和外部 Python Sidecar 模型调用仍未全部覆盖。Whisper 服务未提供用量时不会捏造 Token。
- 在隔离数据库中验证权限、Python 旧值只读回退、Rust 更新覆盖、计价防错、窗口聚合、毫美元差异；这些操作不能替代真实发布/设备或全部 Python 契约验收。本阶段新增账本与资源清理能力均已在隔离测试中验证；接口矩阵另有后续新增，只按最新审计结果统计。

## 2026-11-22：AI 请求观测与 V2 资源保守补扫及管理 API

- `ai_usage.rs` 统一收集 HTTP 传输成功和错误尝试、Token 用量、provider/model 及估算费用；不持久化提示词、回复内容和密钥。RAG 研究 Agent 和投稿规则审核有请求级端到端回归。部分模型接口与外部 Sidecar 的请求事件仍待统一接入。
- `AssetRegistry::reconcile_published_resources` 周期补扫 V2 工作流证据明确、已确认发布、超过用户 TTL 的资产；拒绝活动作业、未知结果、未完成 P2 和 SAU 发布。补扫生成明确事件和最少一小时安全缓冲，零天默认不清理。不会扫描或删除 Python V1 独有旧文件。终态的资源回收仍须检查租约和引用。
- 新增原版 YouDub 任务摘要 `GET /settings/dubbing/jobs/summary`，仅查询真实 Python 任务表中的 `youdub:voxcpm2`；渲染连接 `GET/PUT /render-management/connection` 使用原 Python 的设置格式和单独 Rust 覆盖层。尚未适配独立 GPU resident 控制、远程 Worker 注册/取消、原版 `/subtitle/{path}` 动态服务路由；不返回虚假的在线/完成状态。
- 继续将原版 `GET /render-management/workers`、`GET /render-management/enrollments`、`GET /render-management/executions`、`GET /render-management/executions/{execution_id}` 原生映射为四条只读接口，按原 Python schema 计算心跳陈旧时间、GPU 设备开关、可用槽位和执行记录。无合成 GPU 在线状态，Worker 新增/撤销/执行重排仍待迁移。
- 本阶段矩阵达到 **83/217 部分原生映射、0 项完整签收**；当前最新数据请查看下一阶段与 `CONTRACT_MATRIX.json`。

## 2026-11-22：字幕管理、RenderWorker 真正控制及 Python/Rust 同库差分

- 本轮 Rust 原生映射增加 10 条：`GET/PUT /subtitle/auto/profile` 和 `GET /subtitle/jobs/{job_id}`；RenderWorker 的邀请创建/撤销、Worker 设置、GPU 设备启停、凭证撤销、执行取消与重排。增加的 3 条字幕接口来自原 `subtitle_service`，审计脚本按源文件登记，不再只统计 orchestrator 代理外层的通配路由。
- 字幕自动配置沿用 Python 默认字段，Rust 更新直接写入原 `app_settings`。仅允许已由 Rust 原生字幕引擎实际消费的翻译/ASR 语言字段修改；YouTube 原生字幕优选、摘要、旧 ASR 引擎、自动投稿等未完成能力**明确 409 拒绝**，不会“保存成功但不生效”。
- RenderWorker 管理写操作默认关闭；隔离或审核通过的部署需显式设置 `VIDEOROLL_V2_ALLOW_V1_RENDER_CONTROL=true`，并确认 DB 指向允许修改的实例。事务和行锁保障设备偏好、邀请码、取消/重排。对旧执行的重排存在 owner fencing，重复重排幂等，不会用旧执行清除新租约。
- 此阶段 P2 取消/重排尚未支持 Outbox 联动，因此活动执行拒绝操作。后续 2026-11-23 迁移已经补上原版的事务与事件写入；此行保留为阶段历史，非当前功能状态。
- `rust/tests/python_rust_subtitle_render_diff.py` 使用独立临时 PostgreSQL，直接执行原 `videoroll.apps.orchestrator_api.services.render_worker_service` 的 Python 函数、字幕 `auto_profile_store` 与 `SubtitleJobRead`，通过同初值双行对照 Rust HTTP 及 DB 副作用。动态 UUID/时间仅比较形状或相对 TTL；额外验证 Rust 幂等/新租约 fence。差分通过仍不代表两套生产系统所有业务完全等价。
- 本阶段矩阵为 **93/217 部分原生映射，0 条完整签收**；最新总数见 2026-11-23 更新。真实 Worker 端到端协议、Python 原服务进程级 HTTP 差分、重复重启/崩溃注入和真实平台外部副作用仍待验证。

## 2026-11-23：P2 事件事务、字幕质量/上下文与生产枚举兼容

- `render_management_control_v1.rs` 的 P2 取消现按照 Python 源码使用同一数据库事务：锁定 RenderExecution、RenderJob、DubbingJob，终止执行和原 owner 租约；当配音还处于 rendering 时将 DubbingJob 置 failed，并持久写入原 `outbox_events` 的 `workflow.dubbing.finished`（聚合 ID、workflow task name、`subtitle-control` 队列与幂等 operation_key 保持原值）。重复取消不会重复创建 Outbox。P2 重排会将 paused/failed 配音调整为 rendering_p2，验证任务归属、终态拒绝、旧执行不得清除新 lease，重复重排不增加重试数。仅在显式启用 V1 控制时允许执行；外部 broker 真实投递未测试。
- `GET /subtitle/tasks/{task_id}/quality` 及 `GET /subtitle/tasks/{task_id}/translation-context` 新增只读原生路径，按最新真实 SubtitleJob 的 `request_json.artifacts` 读取质量 JSON 与上下文 JSON，复刻 Python 无资源时 available=false 和上下文 memory 规整。文件读取经绝对 root、相对 key、符号链接防护和 2 MiB 预算；无资产时不会伪造报告。修改上下文及由修改引发的重译仍待迁移。
- **发现并修复 P0 生产 TaskStatus 枚举契约**：只读查询生产 PostgreSQL 显示 `task_status` 标签为 `created/ingested/.../published/failed/canceled`，而非旧测试夹具的 `CREATED/PUBLISHED`。Rust 任务数据库 SQL 已改用原 PostgreSQL 小写枚举，在查询对外结果时显式转换为原 API 的大写 TaskStatus.value；修改 11 个 Rust 代码文件与隔离夹具，不修改生产数据库。此修复涉及任务建立、停止恢复、翻译渲染、发布、历史资产清理、实时事件与前端状态。
- `python_rust_subtitle_render_diff.py` 按生产实际小写枚举重建隔离 PostgreSQL，直接调用旧 Python 函数，验证 P2 状态、Outbox 字段、幂等/lease fencing、Python SubtitleJob ORM、质量报告与翻译上下文真实文件字段一致；读取危险 ../ 路径则明确拒绝。
- 当前 **95/217 条部分原生映射、0 条完整签收**。隔离通过不代表生产 Worker 或真实浏览器投稿通过。生产服务的异常停机/开机恢复应单独处理，不得由开发测试自动恢复或迁移。

## 2026-11-23：重启后的 Python 生产服务与模型提供方列表

- `sudo systemctl start videoroll.target` 完成，原 Python target 及全部列出的依赖子服务成功运行，健康路由返回 200。生产部分并未部署 Rust。主机同时运行 Qwen、ComfyUI 等大型工作负载，编译时 Swap 使用约 9 GiB，后续大规模测试应选择内存余量更充足的窗口。
- Rust 本次增加只读模型提供方目录 `GET /subtitle/translate/model-providers`，归一化内建 OpenAI/Index-Translate 和原版 DB 中自定义模型；不回传密钥原文。源码级原版 Python 差分测试夹具已加入，但尚需启动新编译 Rust API 进行 HTTP 行为验收。
- **96/217 条部分适配、0 条完整业务签收**。本轮 `cargo check --locked --all-targets` 与静态矩阵审计成功；`cargo test` 在生产机器资源紧张时超时。不可将新增接口描述为测试通过。

## 2026-11-23：Rust 自定义翻译提供方新增、编辑、删除

- 新增 `POST /subtitle/translate/model-providers`、`PUT /subtitle/translate/model-providers/{provider_id}`、`DELETE /subtitle/translate/model-providers/{provider_id}`，共 3 条原生路由。连同 GET，均使用源 Python 的 `subtitle.model_providers` JSON 结构；真实 Rust 翻译运行时也通过 `providers::resolve` 读取该表，避免“列表改了但模型未改”的黑盒。本阶段内建 `legacy` 和 `index_translate` 的 PUT 尚未迁移；见 2026-11-23 后续章节，已补原 `subtitle.translate` 读写。
- 所有 V1 Provider 写操作要求显式环境开关 `VIDEOROLL_V2_ALLOW_V1_PROVIDER_WRITES=true`，默认关闭；系统恢复后的生产 Python 服务未开启该开关。Rust 更新原始 `app_settings` 时使用事务、行锁及 Rust 同族写入使用的 advisory lock；**老 Python 并不使用该 advisory lock，混合部署的同时写入仍需运维互斥控制**，不可宣称跨版本完全线性一致。
- 对照 Python 源码保留 `api_key=None` 不变、空字符串清空，以及 `dbplain:v1:` 的可恢复 DB 凭据格式；安全 DTO 只返回 `api_key_set`，绝不回传原始凭据。模型 ID、重复值、温度、Thinking、JSON 安全整数 Token 限额、输出须小于上下文窗口、地址、超时和重试次数均在写入事务前验证。
- `python_rust_subtitle_render_diff.py` 在同一个临时 PostgreSQL 中调用原 Python `save_provider/delete_provider/list_providers` 并与 Rust HTTP 对照，验证创建、修改、密钥保留与清空、非法更新回滚、内建提供方保护、删除、脱敏及真实数据库持久结构。专项差分和 99 接口全隔离回归已通过，日志 `rust/target/v1-99-provider-crud-full-smoke.log`。探测结果 58 条 2xx、40 条 4xx、1 条受限外部投稿 502；其中详情类 404 为夹具缺少 ID 的未签收项。
- 当前 **99/217 部分原生映射、0 条完整业务签收**。远程模型目录发现与内建提供方编辑在此历史阶段尚未完成，见后续章节；ASR/翻译全配置、实际账号模型调用与重启验收仍未完成。

## 2026-11-23：内建模型配置与远程模型目录

- 已将内建 `legacy` 和 `index_translate` 的 PUT 接到真实 `app_settings['subtitle.translate']`：兼容原版 OpenAI/Cerebras 模型地址、名称、推理模式、温度、请求超时及 API Key 语义；Index-Translate 映射温度、Token 预算、上下文字符数、批次大小、术语表、指令及领域。二者不是自定义模型目录行，Rust 按原版路径写入，写操作要求 `VIDEOROLL_V2_ALLOW_V1_PROVIDER_WRITES=true`。
- 原版 `app_settings` 是唯一配置来源，已取消多版本覆盖层的拒绝写入逻辑；仍不能将这种安全保护解释成全部 Python 契约的逐字段复刻。原 Python 更新会顺带规整更多历史 RAG 参数；Rust 当前只迁移这两种内建提供方直接需要的字段。混合 Python/Rust 并发更新同一行仍需运维互斥。
- 新增 `GET /subtitle/translate/model-providers/{provider_id}/remote-models`：源自 Python `fetch_remote_model_catalog`，仅抓取数据库已保存的提供方，读取 OpenAI `/models` 格式，前 500 条、有效 ID 去重、排序截取 200；接受可验证的上下文和输出元数据，拒绝互相矛盾上限。返回模型 ID 和非臆测 Token 上限，不持久化下载结果。
- 对远程目标新增更严格的 SSRF 保护：默认 HTTPS 公网地址，经解析的 DNS 地址检查并由 HTTP 客户端固定，禁用环境代理、HTTP 跳转，15 秒总超时和 2 MiB 响应限制。仅本地隔离回归设置 `VIDEOROLL_V2_ALLOW_TEST_MODEL_CATALOG_LOOPBACK=true` 才允许访问 loopback HTTP mock。这会拒绝某些 Python 以前允许的内网模型网关；内网端点需有显式的安全授权与端点固定策略后再放开，不能直接降低公网安全边界。
- `rust/tests/python_rust_subtitle_render_diff.py` 使用原 Python `_updated_builtin_model_provider`、`fetch_remote_model_catalog`，同临时 PostgreSQL 和本机 Mock 进行差分；核对 OpenAI/Cerebras 及 Index 参数、数据库字段、非法参数/覆盖层拒绝、远程模型排序、Token 元数据、受限地址禁止联网。**100/217 条部分原生适配、0 条生产级全面签收**。
- 完整隔离回归日志：`rust/target/v1-100-provider-full-smoke.log`，退出码 0。100 条接口探测：58 条 2xx、41 条 4xx、1 条外部投稿阻断 502，非预期失败 0；5 条原版详情路由仍缺相关夹具记录，不能视为完成真实等价签收。

## 2026-11-23：ASR 与翻译高级设置 GET/PUT，执行时动态配置

- 新增原版 `GET/PUT /subtitle/asr/settings` 与 `GET/PUT /subtitle/translate/settings` 4 条 native V1 路由，分别直接读写原 Python `app_settings.subtitle.asr`、`app_settings.subtitle.translate`；没有另造独立的前端虚假配置。Rust ASR 默认值对照原版配置类，翻译 57 字段默认响应由原 Python `TranslateSettingsRead` 和 `get_translate_settings` 生成 `rust/src/translate_defaults_v1.json`，不包含实际密钥。写入采用事务，新增 `VIDEOROLL_V2_ALLOW_V1_SUBTITLE_SETTINGS_WRITES=true` 仅用于获授权的 Rust 环境和隔离脚本，默认关闭。
- ASR 设置字段覆盖 OpenVINO、External Whisper、Groq Whisper、Cloudflare Workers AI、VAD、并发/分片及 API Key 保留/清空；写入原 Python 使用的层级 JSON 和 `dbplain:v1:` 密钥格式，读取只返回已设置标志。Python/Rust 同库差分针对默认值、25 字段、多个渠道配置、DB 结构、非法请求回滚均通过。
- 翻译高级设置包含 OpenAI/Cerebras/Index 与 RAG 搜索、Embedding、术语发现、词典及 Agent 配置。57 字段 GET 和有实际数据库副作用的 PUT 经过原版 Python `get_translate_settings/update_translate_settings` 同库差分，验证响应、持久化 JSON 及明文密钥不在 HTTP 返回值中。
- **实际调用消费修正**：原 `providers::resolve` 的内建 legacy 分支过去只使用启动的 `V2_TRANSLATE_*`，忽略 UI 保存的 `subtitle.translate.openai`。本轮改为执行时读取数据库的 base/model/API key/Thinking/timeout/retry，V2 配置环境作为未设置时的回退。Rust Subtitle stage 也在新任务执行时读取 `subtitle.asr.default_engine`；已接通 External Whisper、Groq 的 OpenAI-compatible ASR HTTP。其他引擎继续原有外部 Worker 路径，不能把已有 HTTP Whisper 适配冒充本地 OpenVINO/faster-whisper 或 Cloudflare 原生复刻。
- 2026-11-23 本轮 smoke 将 Worker 启动 ASR 模型故意设为 `stale-startup-model`，通过 ASR 设置 API 写成 `whisper-mock`；同时不提供启动期翻译 Provider 环境变量，通过翻译设置 API 保存 `legacy-mock-translator`。完整隔离流程实际生成包含模拟中文译文的 SRT、ASS 和 FFmpeg 硬字幕，证明 ASR 与翻译配置均进入任务执行链路。
- **104/217 部分原生映射，0 完整生产级签收**。ASR 旧版 v1 自动升级、运行中跨进程设置缓存、完整 Hatchet 队列管理、真实云账号、特殊提供方及真实 GPU / 故障注入验收仍待完成。
- 验收：`rust/target/v1-104-full-native-model-selection.log` 全链路隔离成功，104 条接口探测 62 条 2xx、41 条 4xx、1 条被隔离环境阻断的真实投稿 502，意外失败 0。`rust/target/104-unit-regression.log` 的 Rust 单元/集成测试 104+1=105 项通过。5 条详情 404 因测试夹具缺对象仍未业务签收。临时数据库/模拟服务测试结束清理。

## 2026-11-23：Rust 处理队列及真实下载并发

- 新增原版 `GET /subtitle/task_queue`、`PUT /subtitle/task_queue/settings`、`GET /subtitle/task_queue/broker` 三条 native V1 路由，使用真实 PostgreSQL 租约与源 Python 数据库记录，不依据含糊 Task 状态伪造执行。读取兼容原版有效 `subtitle_jobs`、`render_jobs`、`pipeline_runs` 和 `operation_inbox`；对于 V2 则读取 `vr2_jobs`、`vr2_workflows`、进度与租约，并在同一 UI 列表保留排队、运行、超时待恢复的任务。
- 下载并发设置写入 Python 原本使用的 `app_settings['workflow.youtube_download_concurrency']` 的 `max_concurrent`，保留 1–16 边界及默认 2。**修正关键行为**：Rust `Scheduler::claim` 现在在数据库事务 advisory lock 下执行原子领取，读取此配置及当前有效下载租约（兼顾旧 OperationInbox 的有效下载租约），不再只是前端保存数字；变更只影响新领取，不强行杀掉运行任务。纯 Python 任务仍由原 Python supervisor 独立领取，混合双调度器不保证全局原子限额，生产灰度前需明确所有权。
- Rust 没有 Celery/Kombu。原 `task_queue/broker` 返回兼容字段形状的 `v2:<stage>` PostgreSQL ready/running/awaiting 统计；`worker_observed=false` 和警告明确指出未核实 Worker 心跳、Redis Unacked 不适用，不能把此视为 Python Redis broker 的逐字段等价复制。
- 专项隔离测试与 Python 原函数同库比较 legacy render/subtitle 活租约、Hatchet launch_failed、下载 Inbox、队列优先级、Stage、elapsed、并发配置入库，验证 native V2 pending/running/expired 三态和 broker 计数。多线程原生 PostgreSQL `Scheduler::claim` 额外验证同时领取上限从 1 到 2 的真实效果。
- **107/217 部分适配，0 条完整生产级等价签收**。仍待迁移的是 Python Redis/Celery broker 遥测、旧版自动 YouTube 下载实时进度在队列中的完整映射、真实运行环境混合调度验收。
- 最终隔离验收：`rust/target/v1-107-full-isolated-queue-final.log` 退出码 0，107 API 探测 = 65 个 2xx、41 个 4xx、1 个受限真实投稿 502，非预期失败 0；`rust/target/v1-107-rust-unit.log` 单元与独立测试 105+1+1=107 项通过；并发 Worker 的 `queue_claim_limits` 在真实临时 PG 内执行且通过。原 Python 同库差分通过，测试临时资源清理完成。5 个读取详情路由仍缺有效夹具对象，不视为完整签收。
- 测试过程曾出现一次 Remote RenderWorker 完成后未到终态的非确定性失败；下一次已复测通过。后续生产灰度前仍需加入持续多次稳定性/故障注入回归，不能据一次通过声称已根除历史卡死问题。

## 2026-11-23：手动翻译上下文写入与 Whisper 模型管理

- 新增原 Python `PUT /subtitle/tasks/{task_id}/translation-context`：更新旧 `subtitle_jobs.request_json.artifacts.translation_context_key`、在原共享 storage 生成 `sub/{task_id}/translation_context/manual-<uuid>.json`，存储经原版规则规整后的 `version/summary/memory`；处理旧人物、别名、术语、歧义改动，并根据真实 `segments_json` 识别受影响原字幕行。写入同一行的操作用事务与行锁串行化，不会让并发点击覆盖已经保存的上下文。
- `GET /subtitle/models` 与 `DELETE /subtitle/models/{name}` 已迁移：读取实际 `SUBTITLE_WHISPER_MODEL_DIR`，列出非隐藏目录及递归文件字节数；删除真实目录、缺失返回 404、非法模型名称拒绝。出于目录删除的基本边界要求，拒绝 `..`、绝对路径、软链接目标；正常模型操作均使用原版目录。
- 使用原 Python `put_task_translation_context/get_task_translation_context` 做同数据库和同文件树独立任务夹具差分：核对 JSON 内容、`changed_terms`、边界匹配的 `affected_indices`、SubtitleJob 资产引用及 GET 回读，并验证空记忆不更新 DB。使用原 Python `list_whisper_models/delete_whisper_model` 对照模型清单、嵌套目录体积和真实删除。
- 该专项差分已通过，原 Python 与 Rust 写入结果一致；完整隔离日志另见本阶段最终验收记录。**110/217 条部分原生接口，生产级全签收仍为 0**。
- 模型下载、ZIP 上传、代理测试、faster-whisper/OpenVINO 模型实际运行能力仍未原生迁移；只读列表/删除绝不表示模型管理功能已全部完整。
- 完整隔离回归已连续两次通过：`rust/target/v1-110-full-isolated-smoke-final.log` 和 `rust/target/v1-110-full-isolated-smoke-repeat.log`，退出码均为 0；最后一次 110 条 API 请求执行、66 条 2xx、43 条预期 4xx、1 条禁用真实 B 站投稿的 502，非预期失败 0。`rust/target/v1-110-rust-unit.log` 的 Rust 单元与独立测试 107+1+1=109 项通过。
- 远程 RenderWorker 测试曾偶发检查到错误目标任务：Worker 的 `/claim` 会按全局队列优先级取执行，而原隔离测试未经确认就把首次领取的任务当成刚创建的目标任务。现为目标测试任务分配优先级 100，且对 render_spec.task_id 增加精确断言；此外差分测试会清除自己创建的 V2 任务，避免模拟等待任务被后续 Worker 领取。两次完整回归均跑通远程渲染、P2/SAU 和合约探测；不等于已经完成真实生产环境故障注入。

## 2026-11-23：Hugging Face 模型下载代理实测与 ZIP64 模型上传

- Rust 新增 `POST /subtitle/models/proxy/test`：仅 HTTP(S) `huggingface.co` 及其合法子域可测试；代理可由请求直接选择或从原版 `subtitle.asr.model_download_proxy` 实际数据库配置读取。通过 `reqwest` 真实发出 GET，20 秒超时、不跟随重定向，返回原版 Python 的 `ok/url/used_proxy/status_code/elapsed_ms/error` DTO。使用独立 loopback **forward proxy**，与原 Python httpx 方法比较成功 HTTP 200、拒绝 HTTP 403、非法 URL 拒绝、保存代理自动读取，均通过。
- Rust 新增 `POST /subtitle/models/upload?name=...`：原生 axum multipart 流式收取 ZIP（限 20GiB，单文件名验证），使用 Rust `zip` 2.4.2 的 Deflate/BZip2/LZMA/ZIP64 解压、CRC 校验、100,000 文件和 40GiB 未压缩上限、阻止路径穿越和软链接，并在临时目录完整解压后原子提交。同名已有模型不能被静默覆盖，错误后清理临时上传文件。
- **不是调用 Python 进行解压**：初期的临时 Python zipfile 辅助程序已废弃并清理，归档解析和写盘由原生 Rust 完成。测试中仅使用原 Python `upload_whisper_model` 作为同目录行为对照。真实 multipart 请求与 Python 原实现解出一致权重、文件名与大小；强制 ZIP64 本地头、重复上传、非法名称、错误扩展名及恶意穿越 ZIP 的专项差分已通过。
- 本阶段 ZIP 与代理接口均通过 Python 原函数的隔离差分。模型下载接口随后继续原生迁移；详细适配范围和限制见下节。

## 2026-11-23：Rust 原生 Hugging Face 快照下载

- 新增 `POST /subtitle/models/download`：直接向 Hugging Face 模型 Revision API 请求 `siblings` 文件列表，按原 Python `model_downloads.py` 中的 faster-whisper、OpenVINO、Embedding 模型别名解析原始 repo_id / 本地目录名，按相应 `/resolve/{revision}/{filename}` HTTP URL 流式下载各配置和权重文件；支持 `revision`、`force`、`HF_TOKEN` 和原 `subtitle.asr.model_download_proxy` 配置。
- 与 Python 原版 `download_model_snapshot` 对照：通过模拟 `huggingface_hub.snapshot_download` 的文件写入效果和独立本地 HF REST Hub 服务器，核实模型别名、下载后文件 bytes、默认目录名、版本替换、重复下载报错以及失败的临时目录清理。Rust `force` 在完整新版本写入后才切换，失败时**保留旧模型**，比原 Python 预先删除旧模型的行为更耐故障；该差异必须保留在迁移文档中。
- **113/217 条部分原生适配，0 条完整生产等价签收**。尚未用真实公网 Hugging Face 验证过大模型、断点续传、Xet/LFS CDN、私有模型 token、速率限制、长时间连接和重试，也未复刻 `huggingface_hub.snapshot_download` 的全部缓存逻辑；原生本地 faster-whisper/OpenVINO 推理仍未完成。因此只能声称隔离环境的真实 HTTP 文件下载路径通过，不能声称完全复刻。
- 最终完整隔离验收 `rust/target/v1-113-full-isolated-final.log` 退出码 0：113 条 API 探测，67 个 2xx、45 个预期 4xx、1 个隔离环境拒绝真实投稿的 502，非预期失败 0；`rust/target/v1-113-rust-unit.log` Rust 测试 110 个单元和 2 个独立测试通过，总计 112 项。此前完整回归暴露的队列竞态来自同一临时 DB 中真实 Worker 领取 synthetic 队列任务，已将专项 fixture 的 `available_at` 推至未来并在测试末尾清理；专项差分和完整回归已重新通过。
- 隔离文件目录和 PostgreSQL 容器退出后均已清理；生产 Python 和 Web 健康检查继续 HTTP 200，Rust 生产实例未启动。无真实公网模型拉取或平台投稿。

## 2026-11-23：ASR Provider 连接测试迁移

- 新增 `POST /subtitle/asr/external/test`、`POST /subtitle/asr/groq/test`、`POST /subtitle/asr/cloudflare/test` 三条 V1 原生接口。由 Rust 生成有效 16kHz 单声道、16-bit、1 秒静音 WAV；External Whisper 通过真实 Multipart 上传 WAV、配置中的 `batch_size`、VAD、阈值、静音时长及前文依赖选项，同时复刻原 Python 对 400/422 扩展字段不兼容时退回最小 OpenAI 字段重试；Groq 通过本机 FFmpeg 真正转为 FLAC，再发真实 OpenAI-compatible Multipart；Cloudflare 将 WAV 以 Base64 JSON 发到 `accounts/{account_id}/ai/run/{model}`。
- 3 个测试均读取原 `subtitle.asr` 设置中持久化的 API Key（未传入时），并兼容明确传入的 Key，解码支持原版可读的 `dbplain:v1:`。返回原接口的 `ok/status_code/elapsed_ms/text/segments/error`，External 额外包含最长句长度/秒数；异常时返回失败 JSON 而不是伪造正常结果。
- Python 原 `test_external_whisper`、`test_groq_whisper`、`test_cloudflare_workers_ai` 被用于隔离 HTTP 真实调用差分：本地 mock 分别验证 WAV/FLAC/Base64 WAV、Authorization、模型 ID、翻译结果和字幕段时间，实测 External 的 `batch_size=3`、`vad_enabled=false`、`speech_pad_ms=0` 不是保存后被默默忽略。Groq/CF 原 Python 默认的公网 URL 仅在单次差分内重定向到 loopback，Rust 测试部署使用显式测试 URL 环境变量，生产无覆盖。
- 当前 **116/217 部分原生接口，0 完整生产等价签收**。与 Python 的长音频切片、VAD 语音判断、Provider Redis Gate、指数退避/429 冷却、精细分词/超长句拆分尚未完全复刻；这些诊断路由不是完整 ASR 推理替代。真实 Groq/Cloudflare 帐户与公网 API 未使用。
- 完整隔离验收 `rust/target/v1-116-full-isolated-smoke.log` 退出码 0：116 条 API 探测中 68 条 2xx、47 条预期 4xx、1 条隔离环境故意禁用真实投稿的 502，意外失败 0，8 条受限夹具未计为完整业务签收。`rust/target/v1-116-rust-unit.log` Rust 单元 111 项、独立测试 2 项全部通过（总计 113 项）。另 `rust/target/v1-116-asr-config-focused-diff.log` 证实独立 Python 原函数/真实测试 HTTP 差分全部通过。生产 Rust 未启动，未进行平台/公网模型真实发布。

## 2026-11-23：字幕词典来源、词条与查询操作

- 原生实现 8 条 Python V1 API：`GET/PUT/DELETE /subtitle/dictionaries/sources[/{source_id}]`，`GET/PUT/DELETE /subtitle/dictionaries/entries[/{entry_id}]`，`POST /subtitle/dictionaries/lookup` 与 `GET /subtitle/dictionaries/import-presets`。直接读写 Python 的 `translation_dictionary_sources`、`translation_dictionary_entries`，不是 V2 的附属缓存；保留分页、按来源优先级/质量/更新时间排序、词条启停、来源启停、精确/模糊检索及来源删除时外键级联清理。
- **词典 Lookup 是带副作用的操作**：按原 Python 查询顺序返回调用之前的 `usage_count`，随后在同一个 PostgreSQL 事务内为命中词条 `usage_count += 1`、`last_lookup_at = now()` 和 `updated_at = now()`。原 Python `dictionaries.py` 被作为同 DB 不同领域数据的差分来源，测试对比原字段、源优先级、命中顺序、禁用来源/禁用词条、实际计数、删除级联和已删除资源 404；原 ECDICT import preset 也逐字段对照。
- 写操作在隔离工作树使用 `VIDEOROLL_V2_ALLOW_V1_SUBTITLE_SETTINGS_WRITES=true`；生产 Rust 未部署。原 Python 的 `/subtitle/dictionaries/import`（批量词典解析/导入/历史批次/文件存档）和 `/subtitle/dictionaries/promote`（RAG 知识提升）本轮**未迁移**。目前 Rust RAG 仍有独立知识检索层，不能因为 Lookup API 存在就声称完整的翻译 Agent 词典集成。
- **124/217 条部分适配，0 条生产完整等价签收**。旧 Python 词典表若尚未迁移，需先建立原表，当前 Rust 不偷偷在 API 调用中执行生产 DDL；测试仅在临时 PostgreSQL 使用专门夹具。
- 完整隔离回归 `rust/target/v1-124-full-isolated-smoke.log` 退出码 0，124 条 API 探测全部执行，71 个 2xx、52 个预期 4xx、1 个禁用真实 B 站投稿的 502，0 非预期失败；11 条缺有效对象/隔离限制的探测不视作完整签收。`rust/target/v1-124-rust-unit.log` Rust 单元 112 项、独立测试 2 项全部通过，合计 114 项。原 Python 词典模型实际 SQL 函数与 Rust HTTP 使用同临时 PostgreSQL 中隔离 domain 夹具做读写差分，已通过。

严禁把模拟 SAU、模拟创作中心、原生 HTTP 接口可用或前端构建通过视作真实平台验收。


## 2026-11-23：旧版功能缺口补齐（第 1 批）

- 新增原版 `GET /settings/dubbing`、`GET /settings/dubbing/native`：直接读取真实 YouDub 安装目录、开关、后台原版 / Rust 配音作业与驻留 Unix socket。不会仅凭配置声称 GPU 已加载。**旧版 PUT /settings/dubbing/native 仍未实现，不能用于启停或切换驻留。**
- 新增原版 `GET /subtitle/agent/skills`：按原 Python 技能目录及用户自定义目录、配置开关生成 Skills 列表；隔离 Python registry JSON 逐字段差分通过。
- 新增原版 `POST /render-workers/v1/local-enroll`：只允许私网来源 + Bootstrap Bearer + 显式原版 Worker 控制开关，支持旧 `vrw_` 凭据、重注册限额与拒绝覆盖另一引擎 Worker。
- 新增原版 `DELETE /tasks/{task_id}/assets/{asset_id}`：仅操作 Rust 拥有的 video_final，检查活动工作流、外部 Unknown、渲染/配音与资产引用；事务化解除播控关系、释放自身引用，交给 GC 延迟回收，绝不直接 rm 原版媒体。
- 真实 HTTP + 一次性 PostgreSQL 及实际 GC 的定向专项测试已通过。该指标仍是 V1 路由原生化比例，不是生产级完全等价率。
