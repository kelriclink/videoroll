# 2026-11-25 全接口隔离验收第七轮

基线 `cb7af8d`。新增 Orchestrator 5 条独立正向业务夹具，不改变生产业务代码、原 API 契约或数据库结构。

| 场景 | 总接口 | 正向 | 仅拒绝 | 主动阻断 | 异常 |
|---|---:|---:|---:|---:|---:|
| 上轮独立探测 | 217 | 194 | 22 | 1 | 0 |
| 本轮独立探测 | 217 | 199 | 17 | 1 | 0 |

独立探测退出 0，65 条记录含明确 fixture_asserted。Orchestrator 119/132（12 拒绝、1 主动阻断），Subtitle 55/55，YouTube 9/9，Bilibili 10/10，SAU 5/9，Egress 1/2。正向请求数不是 Python/Rust 完整业务等价签收数，仍为 217 partial native、0 full parity signoffs。

新增 `contract_actions_v7.py`：已有 YouTube 视频和元数据精确回读，重复请求不新建任务；选择性重译使用原生成功字幕阶段、真实对齐快照、持久化 SHA-256、去重行号及竞争请求拒绝，源快照原字节不变；关键词投稿审核实际持久化审核结果和指纹，无投稿副作用；SAU 账号校验及 multipart 凭据导入使用已启用的 loopback 模拟 sidecar。每条新增正向路由先验证匿名拒绝。首次测试错误地读取物理表不存在的 configuration 列，改用既有 vr2_workflows 兼容视图后通过。未修改生产代码。

另实际重跑 `SELECTIVE_ONLY` 专项，退出 0：OpenAI 兼容与 Index-Translate 模拟服务的真实 HTTP、仅重译指定字幕行、保持时间轴和源文件、生成独立 SRT/ASS 修订与新渲染视频，不投稿。专项不重复计数。YouTube 本轮覆盖已有缓存回读，非真实下载；SAU 本轮只补主服务代理，独立 SAU 四条正向仍待接入。未重跑完整 smoke、未验证实际模型/浏览器/远程 GPU 或生产配置。退役 remote GET、旧 dubbing confirm/rerun 仍预期 410，不能把它们作为待启用功能。

隔离 PostgreSQL 35482；日志 `/4t/videoroll-rust-api-parity-actions-target/contract-217-r2.log`、`selective.log`、`contract-parity-217.json/.md`。临时容器、卷和测试端口已检查清理。未操作生产数据库、真实投稿、部署或推送。
