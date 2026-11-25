# 2026-11-25 全接口隔离验收第八轮

基线 `4b5db64`。新增独立 SAU 四条正常业务夹具，保持生产业务代码、数据库结构及 API 不变。

| 场景 | 总接口 | 正向 | 仅拒绝 | 主动阻断 | 异常 |
|---|---:|---:|---:|---:|---:|
| 上轮独立探测 | 217 | 199 | 17 | 1 | 0 |
| 本轮独立探测 | 217 | 203 | 13 | 1 | 0 |

最终全接口探测退出 0，69 条记录有明确 fixture_asserted。Orchestrator 119/132，Subtitle 55/55，YouTube 9/9，Bilibili 10/10，SAU 9/9，Egress 1/2。原矩阵仍为 217 partial native、0 full parity signoffs；不能将正向请求数理解为完整 Python/Rust 业务等价。

新增 `contract_sau_v8.py`：临时独立 SAU 进程在真实 PostgreSQL 和 HTTP 上执行账号 multipart 导入、账号复查、模拟浏览器登录和原生投稿入队。逐条检查匿名拒绝，校验账号最终 valid、会话成功、凭据不泄露、临时 cookie 清理、投稿重复提交仅产生一个原始 publish_jobs 与 native_pending Outbox，不启动投稿 Worker。临时 CLI 仅允许 check/login，非真实浏览器或账号。专门运行后恢复原先禁用 browser 的广域探测进程。

另重跑 TERMINAL、WORKDIR、RENDER_ADMIN 三个独立专项，全部退出 0。终态存储清理保留任务历史，拒绝活动/暂停任务、共享文件、符号链接和配音文件，保护原跨进程锁和幂等；工作目录清理六夹具与 Python 原扫描器差分一致，保留活动及新目录；原生渲染重排原子/幂等，拒绝有效租约、已上传、耗尽重试预算、外部票据、完成任务和 Python 执行，取消释放及旧 Worker 回执防护通过。三个专项不重复计入 203 条，广域探测中的三个路由仍为拒绝路径。

隔离 PostgreSQL 35483；日志 `/4t/videoroll-rust-api-parity-sau-target/contract-217.log`、`TERMINAL.log`、`WORKDIR.log`、`RENDER_ADMIN.log` 和逐路由 `contract-parity-217.json/.md`。容器/卷/测试端口清理检查为空。未运行完整 smoke、真实账号登录/投稿、生产配置、生产 DB 修改、部署或推送。剩余 14 条非正向路由中 3 条预期退役 410，其余仍需按有效操作和安全拒绝分别验收。
