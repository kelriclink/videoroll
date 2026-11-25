# 主服务 Bilibili 单目标原生投稿接入（2026-11-25）

基线 `0764f2e`。原 `/api/v1/tasks/{task_id}/actions/publish` 的 Bilibili 分支此前只接受保存的元数据和默认账号，通过普通 workflow restart 排队；显式账号、视频、封面、双 P、分区模式都被拒绝。独立 Bilibili 发布服务已实现这些能力，但原 TaskDetail 入口未接入。

本轮把 `bilibili_publish_v1::enqueue` 提取为两个原入口共享的执行事务，主服务使用 `enqueue_task_action`，保持独立服务显式 batch_id 行为。原 TaskDetail 请求支持指定账号 UUID、显式或最新 final 视频、可选任务所属封面、双 P `video_parts` 以及 meta/bilibili_predict/ai_summary 分区模式（包括 `platform_options.bilibili.typeid_mode`）；默认 mode 为 meta，保持此前主服务不主动切换分区的行为。返回原 publish_jobs UUID、平台、state、aid、bvid、response，不再仅返回 workflow stage ID。现有已保存元数据不允许通过提交偷偷覆盖，必须先保存新草稿；未实现原 Python 的所有自动标签、元数据文件副作用及隐式账号/素材选择边缘规则。

在同一原 Task 行锁和事务下，校验原资产所有权/文件、成功配音证据、账号凭据及审核；新单目标批次写入原 publish_batches 和 tasks.active_publish_batch_id，原 publish_jobs、native workflow/stage 和旧 recovery fence 原子提交。历史同目标 submitting/submitted/published/unknown 行直接返回其已有回执，不把旧 job 重新归入新批次；已失败/草稿或孤立同目标批次拒绝重新提交，等待人工核验。新批次拒绝已有无批次投稿历史和旧 SAU facade intent；当前 publishing/published Task 若无对应现有回执仍拒绝新单目标投稿。`skip_review`、`force_retry`、未知 platform_options 继续拒绝，不开启原 Python 的危险重放选项。没有增加表、迁移或 API 路由。

## 实际隔离验收

`BILI_DIRECT_ONLY=true`（原脚本的 `VIDEOROLL_V2_TEST_BILI_DIRECT_ONLY`）通过，29 个 PASS 行。新增 4 组主服务断言：

- 默认账号、最新 final、三次并发入队共用一个原批次/原稿件 ID，实际 loopback UPOS/creator POST 一次，最终原批次 succeeded、相同 HTTP 回执，无鉴权拒绝；独立服务不允许把该回执重新归入无批次提交。
- 指定账号、视频、封面及 mode 覆盖，mock 检查实际 Cookie/CSRF、cover upload；拒绝外部任务资产、错误账号、审核绕过、强制重试和未知选项，拒绝时不创建批次。
- 双 P 拒绝无成功 YouDub 原生证据，补齐原 operation_inbox 成功记录后实际模拟两次媒体上传、一次稿件提交，保留顺序和原批次结算。
- 模拟 archive POST 503 后原稿件 unknown，重复请求沿用原 UUID，不重放，原批次 failed；历史 draft 拒绝新提交。

同轮全量独立探测 `CONTRACT_217_ONLY` 退出 0，204 正向、12 拒绝、1 主动阻断、0 异常；广域探测不打开 Bilibili 真投稿开关，主服务 Bilibili 分支由上述专项证明。cargo fmt --check、Python py_compile、git diff --check 通过。

## 仍待处理

混合平台 publish_all **未迁移**：旧 SAU facade 仍用独立 intent 而非原统一批次，不能把两个执行器的独立请求拼装成成功批次。现有无批次历史检查不是所有入口的共享并发锁，独立 SAU admission/TaskDetail concurrent mixed calls 需要下一轮共同批次所有权和锁顺序验收，不据此进行生产切换。默认 Bilibili 凭据仍沿用既有实现（没有新加默认 Cookie 轮换指纹），同一目标重复请求复用旧回执而非新元数据请求指纹比较；真实平台、实际生产配置、历史任务接管、全部业务差分及本轮完整 smoke/前端尚未验证。未实际登录或投稿、未修改生产数据库、未部署或推送。

日志 `/4t/videoroll-rust-manual-bili-native-target/BILI_DIRECT.log`、`CONTRACT_217.log`、`contract-parity-217.json/.md`；独立 PG 35485。
