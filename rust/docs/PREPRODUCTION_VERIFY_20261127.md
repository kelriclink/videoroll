# 2026-11-27 Rust 预生产验收

基线：a494d5445f85817e9042a191c32f7f660f019daa。所有写操作仅在隔离环境，生产库仅只读查询；没有切换服务、真实投稿或停止线上 GPU 模型。

## 验收证据

- NATIVE_DEPLOYMENT、TASK_OWNERSHIP、DIFF、RELIABILITY、CONTRACT_217、RENDER_EVENTS、SAU_NATIVE_WORKER、YOUDUB_CONTROL、PLAYOUT_ACTION 九组隔离测试退出码均为 0。54 项 Python/Rust 差分通过；217 条探测中 204 条正向、12 条仅拒绝、1 条阻塞、0 条失败，不代表全部业务等价。
- 正式 deploy/rust-native/build-web.sh 构建成功；Chromium 真正初始化管理员、登录、访问 21 个页面、切换 SRT-only 并保存刷新，原生工作流页面不嵌入 Hatchet。无捕获的 JS 异常，浏览器 HTTP 诊断列表为空（采集范围为 /api/v1/）。66 个前端单元测试通过。不是所有控件或全部外部依赖的端到端验收。
- ffplayout 2.7.1 实际二进制、独立 SQLite、测试媒体与 Nginx，完成权限、节目单读写、裁剪排序、频道启停和当前节目查询；输出 null，未实际推流。删除后查询按原生 204 契约。新增可重复运行的 ffplayout_real_isolated.py。
- 串行 Agent 503 注入、恢复与真实 PG 租约/预算/语料版本专项通过；并行 Agent 隔离回归通过；Rust lib 270/270 通过。模型实报用量、配置定价估算和请求前保守预留分开持久化，超额追加，失败不退款；模型凭据指纹、符合检索范围的知识行版本及过期状态参与缓存失效。历史缺失计数不可反推，缺失用量/定价不视为零成本。不是精确 tokenizer、真实账单或美元硬上限；语料指纹在 run 入口检查，不保证运行中原子撤销或无碰撞；查询有 5 秒上限。无新增数据库迁移。
- 日志：/4t/vr2-preprod-20261127.pCoSUy/，其中 BROWSER_NATIVE.log 为正式构建复验结果，AGENT_SERIAL.log、AGENT_PARALLEL.log、LIB.log 为最新 Agent/库测试证据。原始预览构建曾有 400/502 诊断；正式构建复验无该诊断，不据此宣称所有接口均成功。

## 生产只读核查与剩余阻塞

- 5083 个原任务：published 3780、failed 815、canceled 484、downloaded/rendered 4。Hatchet launch_failed 353，published/running 各 1；Outbox pending 3。60 条未确认外部结果：B 站 unknown 24、抖音 unknown 10、抖音 submitted 缺外部回执 26。不能盲目重新提交。
- 原库 collation 记录 2.41、实际 2.36；切换前需维护窗口备份、验证索引、按计划修复。11 月 25 日全量恢复演练并非今日生产切换。
- V100 32 GiB，查询时空闲约 6596 MiB，低于实际 YouDub 验收所需 12000 MiB；不停止生产模型。YouDub、SAU、B站真实平台发布与远端 GPU 联调仍待受控验收。
- 现有隔离并发/故障用例已通过，未执行多小时持续压测。磁盘根分区约 94%，构建和长时测试需单独控制空间。
