# YouTube 原字幕优先迁移（2026-11-25）

基线 `c2e32b9`。原 Python `youtube_downloader.py` 的语言匹配、人工目标字幕优先、自动目标字幕次之、auto_source 仅自动字幕（original/default/audio/language hint、-orig、单轨及排序回退）由 Rust `media/youtube_subtitles.rs` 实现。原手动 subtitle action 与独立 POST /subtitle/jobs 接受 off/target/auto_source 及 prefer_youtube_subtitles，明确模式优先于布尔兼容值；缺省 target，未知显式模式拒绝。自动任务读取原 subtitle.auto_profile 与 workflow.subtitle 覆盖。没有新增路由、表或迁移。

复用现有 yt-dlp 可执行文件、代理/独立 Cookie 副本、compatibility extractor 设置，固定单视频 URL、排除 translated_subs、精确匹配语言、禁 playlist、限制 stdout 和子进程执行时间，不直接请求元数据中的轨道 URL。仅接受临时目录的非空普通轨道文件，实际 FFmpeg 转 SRT，校验视频 ID、字幕数量/大小、实际视频时长、时间戳和文本；没有可用轨道、探测/下载/转换失败或无效字幕回退 ASR。目标字幕在解析 ASR 配置前处理，无音频也可使用，跳过翻译与双语重复；auto_source 按原翻译开关进入现有 RAG/翻译流水线。off、本地、选择性重译不探测。原字幕按 source URL/媒体 SHA256/模式/目标语言写 job-local checkpoint，阶段结果与 segments.json 保存 language/source/reason。复用原 SRT/ASS/segments asset 注册；保留原已存在 SRT 的复用策略。

## 验收

`VIDEOROLL_V2_TEST_YOUTUBE_SUBTITLES_ONLY=true` 退出0：真实 Rust API/Worker、FFmpeg/MP4/VTT、隔离 PG、无网络 yt-dlp fixture 和 loopback ASR。12 个任务覆盖手动目标字幕（无音频/无 ASR 配置）、自动目标字幕、自动源字幕进入 mock 翻译、流水线默认 target、无轨道/探测失败/下载失败/空文件内容/越界/错误视频 ID 的 ASR 回退、off 与 local 不探测；前四个 ASR 调用0，后八个 ASR 调用8，核验 SRT 文字及原始 provenance。`CONTRACT_217_ONLY` 退出0：204 正向、12 拒绝、1 隔离主动阻断、0 异常；`SELECTIVE_ONLY` 退出0（三个 PASS，包括 Index-Translate 模拟调用及重渲染）；`CLOUDFLARE_ASR_ONLY` 退出0（真实 PCM、429、并发1、精确偏移）；`cargo test --lib youtube_subtitles` 1 passed；fmt、py_compile、bash -n、git diff --check 通过。日志 `/4t/videoroll-rust-youtube-subtitles-target/{YOUTUBE_SUBTITLES,CONTRACT_217,SELECTIVE,CLOUDFLARE_ASR,unit}.log`，隔离 PG35490。

## 限制

未连接真实 YouTube/实际字幕轨道、生产代理 Cookie、真实翻译服务；没有模拟进程在 checkpoint 写入后断电，未验证滚动自动字幕重叠、超长字幕、源语言翻译关闭及所有地区语言别名的真实样本。现有 SRT 重用策略可能在切换配置时仍复用既有结果，需后续原任务 resume/artifacts 契约收尾；字幕下载文件预算在下载完成后检查，不是下载期间磁盘硬配额。未测试所有 worker gateway/ASR 配置组合或完整 smoke；无生产库写入、实际投稿、部署、推送。
