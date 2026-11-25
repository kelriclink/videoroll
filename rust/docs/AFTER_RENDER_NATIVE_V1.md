# 独立字幕显式 after_render 投稿（2026-11-25）

基线 `95620d7`。原 POST /subtitle/jobs 接受 after_render.publish=true 及原 PublishAllRequest 的 publish_payload/payload 两种别名（冲突拒绝），显式 payload 要求手动 runtime_profile=false 和原生 mixed/native-only/SAU 开关。明确目标、账号、meta、platform_meta/options、cover、分区及可选 video_key 持久化于原 workflow configuration；显式请求禁自动配音，不随全局自动投稿开关/目标变化。runtime_profile=true 无显式 payload 的标记继续使用原自动配置语义。保留空/禁用 after_render；禁止强制重投、审核绕过、fresh_batch、预选 P2 和选择性重译自动投稿。要求原媒体有资产 SHA/size，冻结原始输入并在 subtitle/render/publish 复查；本地及远程渲染使用本代成功字幕回执（no_speech 不借用旧字幕），不得复用前代 video_final。仅本代成功 render receipt 的输出可发布，显式 video_key 如提供须完全匹配；核对文件实际 SHA/size、原资产/live storage/workflow ref；远程还需原 completed execution/asset/output_json 证明。调用现有原子 batch admission，允许单 Bilibili 父阶段复用批次结算；保留原审核、账号有效性、原生 outbox 与人工核验，失败/未确认目标不会提前完成父流程或清理。新字幕/投稿阶段清除旧显式计划；未新增表、迁移或 API。

## 验收

`BILI_DIRECT_ONLY=true MIXED_PUBLISH=true` 对应隔离 smoke 开关最终退出0，37个 PASS 标记。实际原独立字幕 API -> Rust Worker -> 本地 render -> Bilibili loopback 和无网络 SAU CLI：显式混合、单 Bilibili、payload 别名、同请求重复、不同 metadata 冲突、无效/绕过/强制重试拒绝、全局关闭且改目标不影响显式请求、旧 video_final 不复用、指定旧视频阻断、人工确认父结算；两次模拟 Bilibili submit、一次 SAU CLI，无重复。追加实际失败 FFmpeg（旧 source 快照生成字幕后渲染失败）、入队后原视频篡改：两个任务均 failed，0 publish stage、0 batch/job。第二轮负向用例误查物理表 attempts 列而中断，改用原兼容视图后最终全过。`CONTRACT_217_ONLY` 退出0：204正向、12拒绝、1主动阻断、0异常；`YOUTUBE_SUBTITLES_ONLY` 20任务符合预期，`RENDER_EVENTS_ONLY` 通过。实际编译、fmt、py_compile、diff check 通过。日志 `/4t/videoroll-rust-after-render-target/{AFTER_RENDER-r3,CONTRACT_217,YOUTUBE_SUBTITLES,RENDER_EVENTS}.log`，隔离PG35493。

限制：本轮不是全部 Python payload 等价：仅支持明确目标的安全子集，SAU 必须显式账号和元数据，不支持省略目标隐式扩展、旧配置自动默认账号选择、显式 P2、部分平台成功即通过、强制重投或审核绕过。B站单目标也需统一批次开关；原自动 B站单目标路径不变。账号凭据仍在执行时校验而非冻结凭据版本，部分外部/文件 TOCTOU 风险仍需验收。新远程回执绑定已实现且通用远程事件回归通过，但未独立执行远程 after_render 端到端，真实 GPU/平台/完整生产配置及完整 smoke 未验收。无生产库写入、真实投稿、部署或推送。
