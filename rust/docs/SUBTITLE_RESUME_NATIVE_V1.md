# 原字幕快照恢复（2026-11-25）

基线 `649c9ca`。原独立 POST /subtitle/jobs 支持 resume=true 以及 source_segments_key/final_subtitle_segments_key，原 task subtitle_resume action 自动选择同一原媒体的最近合格历史 job。仅恢复原 Python subtitle_jobs 中 failed/succeeded 且确切 input.key/artifacts 匹配的记录；不接管仍活动的 Python 任务。输入仍由原调度器要求 Rust task ownership 和 V2 原资产或已完成历史接管证明。源字幕必须有同任务 segments_json 资产及原 SHA-256/size，原视频同样要求原资产 SHA/size；最终快照可没有资产行，但必须有同一个原 job request 指针、原 task 路径及 Python 内容摘要前缀，完整实际 SHA 冻结在新 native workflow 的 subtitle_resume_plan。历史源视频关联基于原 job input.key 与当前原资产记录，并非旧 job 自带的独立完整视频摘要。拒绝跨任务、非原格式、无证据或变更内容。执行时再次校验原 job、原媒体和快照；8MiB/100000 cues/时间轴范围检查。原 Python start/end 秒数转毫秒，保留 secondary_text。source-only 跳过 ASR 并继续原生翻译；final 跳过 ASR/翻译，生成新的 native SRT/ASS/segments，保留旧文件。原 native metadata 保存原始 source、translated、resume provenance 和 source_video_sha256；未对齐最终快照不冒充可选择性重译的对齐数据。新恢复版本禁止复用旧 video_final；本地/远程 render spec 绑定本代成功 subtitle receipt 的原媒体和 SRT/ASS，重新核验资产摘要。未增加数据库表、迁移或 API。

顺便修复既有 executor 在未配置 YouDub/worker gateway 时即使关闭 auto_dub_p2 仍走 remote 的问题；配音禁用或本代 subtitle 明确 no_speech 均直接 skip，再由原流程进入 publish/cleanup。原 native/manual 配音配置选择继续使用 dubbing::auto_enabled。

## 验收

`YOUTUBE_SUBTITLES_ONLY` 第二轮退出0：原16个任务加3个成功历史恢复任务及1个入队后篡改拒绝任务（另外测试外任务快照、resume=false 拒绝及重复 admission）。实际 Rust API/Worker、旧格式 JSON fixture、真实 MP4/FFmpeg；source-only mock translation、final 保留原译文和 secondary_text、原任务 action 自动选择、生成新视频、全部3个父 workflow succeeded、禁配音/投稿时不创建稿件；ASR/yt-dlp 调用数量不变，旧快照仍在。篡改夹具 max_attempts=1，验证终态 failed 且没有字幕资产。首轮真实快照校验已拒绝，但原调度重试退避导致测试等待超时，同时发现禁配音仍走 gateway，已修复并重跑。`CONTRACT_217_ONLY` 退出0：204正向、12预期拒绝、1隔离主动阻断、0异常；`SELECTIVE_ONLY` 退出0（4 PASS）；`RENDER_EVENTS_ONLY` 退出0（原远程渲染回执恢复、暂停恢复、异常资产保护等）。实际编译、fmt、py_compile、git diff --check 通过。日志 `/4t/videoroll-rust-subtitle-resume-target/{RESUME-r2,CONTRACT_217,SELECTIVE,RENDER_EVENTS}.log`，隔离 PG35492。

限制：当前支持有原请求证明且具有源字幕快照的历史恢复；仅旧 SRT、缺 SHA/size、历史原媒体已清理、非标准旧快照路径、原分离 translation_context_key/YouTube provenance 等不在本轮兼容范围；独立普通 native resume 仍有未覆盖路径。非本轮恢复任务的旧 SRT 复用逻辑保留。新远程 render spec 绑定已实现并通过通用回归，但没有专门让实际远程 GPU Worker 消费历史恢复任务；未验证恢复过程中源文件原地变更的全部 TOCTOU 情况、真实断电或生产数据全量恢复。通用无语音配音 skip 尚未独立完成远程端到端验收。显式 after_render.publish=true 未迁移；未执行完整 smoke 或真实生产配置验收。没有写生产数据库、真实投稿、部署或推送。
