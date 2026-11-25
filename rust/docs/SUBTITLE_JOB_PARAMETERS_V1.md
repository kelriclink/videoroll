# 独立字幕任务参数兼容第一轮（2026-11-25）

基线 `518194d`。原 POST `/subtitle/jobs` 支持显式 runtime_profile true/false，null/缺省按原 origin_kind 和旧 created_by 首段自动任务标记推断。运行时模式不冻结请求内字幕/渲染参数；运行时 ASR 读取最新 profile 引擎/模型，字幕/翻译仍复用原生配置读取，渲染采用当前原 UI 默认值及设置。新 standalone generation 清除前代 subtitle/render、selective plan、dubbing/publish overrides，自动模式由当前全局自动投稿/配音配置驱动，手动模式禁自动投稿/配音。显式 after_render.publish=false 仅代表无额外手动投稿请求，不覆盖自动模式的全局自动投稿开关。原接口兼容 active stage 同配置幂等。未复刻所有 Python runtime profile 每个中间子阶段的重复设置读取及全字段默认规则。

独立 selective_retranslate 接入原 `/tasks/{id}/actions/subtitle_retranslate` 的 schedule：原 indices、可选 base_translated_segments_key 必须为同任务成功原生 aligned segments.json；artifacts 中 source_segments_key/final_subtitle_segments_key 若给出必须匹配同一快照。保留源任务配置和摘要/SHA/时间轴校验；复用原 worker，不重跑 ASR，不新增 schema。显式 true runtime profile 与原 Python 分离 translation_context_key 拒绝；原生重译使用既有源配置而非覆盖请求的模型/渲染参数。普通非 selective artifacts 及已有 SRT 的独立 resume 仍保守拒绝。原 Python worker 不读取 output_prefix 决定输出目录，现接受空值和原编排器标准 sub/<task_id>/，继续使用 Rust 原 task/job 路径，其他自定义 prefix 不接受。after_render 空对象/禁用且无实际 payload 接受，真实 publish=true/非空 payload 仍拒绝，待独立绑定明确投稿参数及渲染完成事件。未新增数据库表、迁移或 API。

## 实际隔离验收

`YOUTUBE_SUBTITLES_ONLY` 退出0：既有12任务，加4任务（显式 true、自动来源推断、显式 false、运行时 ASR 模型），真实 Rust API/Worker、FFmpeg、模拟字幕和模型；16任务全部通过，4个 PASS 标记。原配置入队后变更被自动 worker 读取，手动保持请求，上一代错误模式/发布/配音配置不继承，原 prefix 接受/外任务 prefix 拒绝，同配置重复入队复用；运行时 ASR 实际 multipart 使用 runtime-whisper 和 en。`SELECTIVE_ONLY` 退出0：4个 PASS 标记，独立入口与任务入口共享原 native job、外任务 artifact 拒绝、真实选中字幕重译、原快照不变、SRT/ASS 和渲染新版本、Index-Translate 模拟编号 API。`CONTRACT_217_ONLY` 退出0：204正向、12预期拒绝、1主动隔离阻断、0非预期异常；并非217条完整生产等价签收。Rust实际编译、fmt、Python py_compile、git diff --check 通过。日志 `/4t/videoroll-rust-subtitle-parameters-target/{YOUTUBE_SUBTITLES,SELECTIVE,CONTRACT_217}-r2.log`，隔离 PG35491。

仍待普通 resume/artifacts 的旧分离快照格式、精确原媒体绑定、真实 after_render 投稿 payload 与安全阶段结算、更多 subtitle output/字体缩放/glossary_id、跨代重试/真实断电、真实自动 profile 投稿端到端、完整 smoke 和真实生产配置。未真实登录、投稿、生产数据库写入、部署或推送。
