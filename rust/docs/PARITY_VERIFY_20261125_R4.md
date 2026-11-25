# 2026-11-25 全接口隔离验收第四轮

基线 `8548dc1`。新增 13 条 RenderWorker 原 `/api/render-workers/v1` 路由独立正向夹具、4 条独立 Subtitle Service 正向业务夹具，修复一个实际 Rust 业务缺陷。未增加物理表或数据库迁移。

| 场景 | 总接口 | 正向 | 仅拒绝 | 主动阻断 | 异常 |
|---|---:|---:|---:|---:|---:|
| 上轮独立探测 | 217 | 154 | 62 | 1 | 0 |
| Worker 修复后独立探测 | 217 | 167 | 49 | 1 | 0 |
| 最终独立探测 | 217 | 171 | 45 | 1 | 0 |
| 本轮完整 smoke | 217 | 165 | 51 | 1 | 0 |

首次有效 Worker logs 请求返回 HTTP 500：原 `render_executions.log_tail` 允许 NULL，新 Rust 执行由原物理表创建时没有非空默认值，`String` 解码失败。修改为 `Option<String>` + 空串默认，保持原表定义及日志长度限制。实际隔离测试先断言首次日志前 NULL，再验证两次追加、保留进度和 metrics。此次没有改变日志格式或长度限制；与原 Python 日志截断规则完整等价仍需单独核对。

13 条 Worker 断言覆盖原路径注册、local-enroll、heartbeat、claim、execution heartbeat/progress/logs、cancel 查询、Range 素材读取、真实 MP4 multipart output、output probe、complete 和 fail。真实 PostgreSQL、真实 HTTP 及文件字节，验证一次性邀请、无凭据拒绝、无空闲槽位拒绝再次领取、错误 fence、原物理表进度/日志、SHA-256、已上传资产、完成后释放租约、重复完成及失败 stage 结算。使用模拟 Worker 以及真实生成的短 MP4，不代表真实 GPU 渲染质量或原 Python Worker 全协议等价；local-enroll 仍为明确启用旧兼容控制的 Python Worker 入口。

独立字幕服务新增 native POST jobs (持久化原 operation_inbox 字幕 stage)、dictionary lookup (准确原 entry 及 usage_count)、CSV dictionary import (原源与术语记录)、JSON knowledge import (原知识行) 四条正向业务断言。保留原服务鉴权、无外部真实平台调用。B 站、SAU 独立服务剩余正向场景和实际浏览器/上传仍未补齐。

完整 smoke 退出 0，共 155 个 PASS 行，其中 54 个 PASS DIFF 行；包含原生 P2、SAU mock、真实 FFmpeg、ASR/Embedding 模拟服务、RenderWorker 及原版同夹具差分。独立与完整探测使用不同任务状态，两组不能相加；正向请求数、PASS 标记与全业务等价签收不是同一指标，矩阵仍是 217 partial native、0 full parity signoffs。

日志及逐路由报告：`/4t/videoroll-rust-api-parity-worker-target/contract-217-r3.log`、`contract-parity-217-standalone.json/.md`、`full-smoke.log`、`contract-parity-217.json/.md`。独立 PostgreSQL 35479。未执行真实生产配置、真实账号登录或投稿、实际远端 GPU、前端构建。未修改生产数据库或部署。
