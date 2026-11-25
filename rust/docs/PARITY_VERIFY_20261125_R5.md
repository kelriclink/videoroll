# 2026-11-25 全接口隔离验收第五轮

基线 `f9c16a7`。新增独立字幕服务 5 条、独立 B 站服务 4 条正向业务夹具，均使用真实服务 HTTP、临时 PostgreSQL、实际文件和 loopback 模拟上游；未修改生产业务代码、数据库表或迁移。

| 场景 | 总接口 | 正向 | 仅拒绝 | 主动阻断 | 异常 |
|---|---:|---:|---:|---:|---:|
| 上轮独立探测 | 217 | 171 | 45 | 1 | 0 |
| 本轮独立探测 | 217 | 180 | 36 | 1 | 0 |

最终探测退出 0，其中 46 条有明确独立业务夹具断言。独立服务：Orchestrator 100/132（31 拒绝、1 阻断），Subtitle 55/55，YouTube 9/9，Bilibili 10/10，SAU 5/9，Egress 1/2。此为正向请求覆盖率，不等于完整 Python/Rust 业务等价签收，矩阵仍为 217 partial native、0 full parity signoffs。

新增 `contract_services_v5.py`：External Whisper 和 Cloudflare ASR 实际模拟 HTTP 请求及识别结果/分段数量，OpenAI 兼容翻译实际 HTTP 及返回 DTO，Hugging Face 小模型快照下载及文件字节，ZIP 上传解压及字节校验；B 站作者账号身份、分区树、AI 分区推荐和原 `publish_jobs`/`operation_inbox` 持久化的提交意图、重复请求幂等。每个新增路由先验证匿名请求拒绝。仅在独立 B 站服务进程启用 native publication，作者中心限定本地模拟，拒绝上传/投稿请求；不启动真实上传 Worker。临时设置在单服务退出后恢复。首次探测因测试错误地将原 ASR `segments` 数量字段当作数组导致断言失败，按原契约改为整数后重跑通过；未改动服务实现。

此外复跑 `SAU_NATIVE_CHECK`、`SAU_NATIVE_LOGIN`、`SAU_NATIVE_PUBLISH`、`SAU_NATIVE_WORKER` 四组专项，全部退出 0：账号凭据保存/刷新、并发校验去重、登录创建与取消、实时事件和重连、三平台投稿原始 job/outbox、事务回滚、实际模拟 CLI 执行、未知结果禁止重复提交、Worker 中断恢复、Python 调度器排除。专项不重复计入 180 条，独立全量探测仍保守禁用 SAU browser 操作。未重跑完整 smoke（上一轮 155 PASS、54 PASS DIFF）；未验证真实平台凭据、外部网络、实际模型推理、真实 GPU 或生产配置。Hugging Face 文件为本地小型夹具，不是实际模型权重。

验收日志 `/4t/videoroll-rust-api-parity-services-target/contract-217-r2.log`、`contract-parity-217.json/.md`、`SAU_NATIVE_CHECK.log`、`SAU_NATIVE_LOGIN.log`、`SAU_NATIVE_PUBLISH.log`、`SAU_NATIVE_WORKER.log`。隔离 PostgreSQL 35480；临时容器、卷、测试监听端口清理检查为空。未部署或推送。
