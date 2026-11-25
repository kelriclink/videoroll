# 2026-11-25 全接口隔离验收第九轮：专项证据收尾

基线 `65b5e2c`。本轮只修改验收脚本与报告，不修改生产代码、数据库结构或接口契约。原 `/api/v1/auth/setup` 在全新隔离库上实际完成初始化并验证重复初始化 409；YouTube 代理诊断向本地拒绝 CONNECT 的模拟器发起合法目标请求，验证 HTTP 200 的故障 DTO（ok=false、状态码为空、错误脱敏）及非法目标 400，不访问实际 YouTube，不声称外网连接成功。

| 场景 | 总接口 | 正向 | 仅拒绝 | 主动阻断 | 异常 |
|---|---:|---:|---:|---:|---:|
| 上轮独立探测 | 217 | 203 | 13 | 1 | 0 |
| 本轮独立探测 | 217 | 204 | 12 | 1 | 0 |
| 本轮完整 smoke | 217 | 198 | 18 | 1 | 0 |

独立探测有 70 条明确 fixture_asserted。所有八组脚本执行均退出 0：CONTRACT_217、TERMINAL、WORKDIR、RENDER_ADMIN、HOME、YOUDUB_CONTROL、FULL、EGRESS。完整 smoke 159 个 PASS 行，其中 54 个 PASS DIFF 行；两组探测结果因任务状态不同，不合并 2xx 数量。终态及工作目录清理保护活动任务、共享资产、符号链接与原锁；渲染重排保护有效租约和原票据；首页扫描验证动态 Browse、分页、过滤、跨进程锁及异常恢复；YouDub 控制验证模拟模型、原文件锁、模拟常驻进程和活动任务保护；Egress 专用测试 feature 验证限定 loopback 上游的字节响应、SSRF 拒绝、重定向验证、敏感头清理及大小限制。

新增 `contract_closeout_v9.py`：保留原 broad 探测结果，从本轮已成功完成的专项日志中匹配明确 PASS 证据，记录日志 SHA-256 和场景范围，按 service/method/path 去重。实际聚合结果：204 broad_positive、10 specialized_positive、3 retired_410、0 unresolved。即 214 条未退役路径存在至少一个有效业务请求证据，3 条预期退役路径验证 410。10 条专项为初始化、终态清理、工作目录清理、渲染重排、YouDub 设置、首页扫描、主服务投稿及 publish_all、P2 追加、Egress fetch。主服务投稿正向证据为模拟 Douyin 分支，Bilibili 分支在 broad 环境仍主动阻断；独立 B 站服务验收不替代该分支。publish_all 证据限多 SAU；Egress 正向限专用测试构建。报告脚本核对日志标记而不独立验证进程退出，必须在调用者确认各 suite 退出 0 后执行。本轮退出状态已由同一连续 shell 任务确认。

**不能将 217 个路由归类视作 217 条完整等价签收**。矩阵仍为 217 partial native、0 full parity signoffs。后续优先补全主服务 Bilibili 分支/混合平台、真实部署配置与数据库克隆环境的全流程测试，以及历史任务所有权接管；实际平台凭据、实际 GPU、完整前端及本轮生产 Egress 非测试构建的安全专项未执行。未修改生产数据库、真实投稿、部署或推送。

证据目录 `/4t/videoroll-rust-api-parity-closeout-target/`，八组日志 `CONTRACT_217.log`、`TERMINAL.log`、`WORKDIR.log`、`RENDER_ADMIN.log`、`HOME.log`、`YOUDUB_CONTROL.log`、`FULL.log`、`EGRESS.log`；逐路由独立与完整报告 `contract-parity-217-standalone.json`、`contract-parity-217.json`、聚合 `contract-closeout-217.json`。隔离 PostgreSQL 35484；临时容器、卷和测试端口已检查清理。
