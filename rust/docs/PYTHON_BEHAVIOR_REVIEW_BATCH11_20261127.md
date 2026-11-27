# 第十一批：全仓 Python 清单、执行所有权与媒体成果一致性

基线：60d8c41。全部 git 跟踪的 Python 文件已读取字节、计算 SHA-256、解析 AST：1202 文件、238070 行、10548 个函数/类定义、0 语法错误（当前审查工作树）。第一方业务 201 文件/65413 行/2710 定义；部署及模型桥接 15/2617/110；历史迁移 14/1168/37；测试审计 234/47142/2841；第三方快照 738/121730/4850。
清单：PYTHON_SOURCE_INVENTORY_20261127.csv。复现：python3 rust/scripts/audit-python-behavior.py --all-python --out /tmp/python-inventory.json --file-manifest /tmp/python-inventory.csv。
**AST 索引不是逐函数语义签收。** 此轮检查了各业务目录的定义清单，并进一步阅读下面的具体实现。清单逐文件保留未签收状态，避免把扫描过文件当作行为等价。第三方项目只审查依赖边界，不重新实现其业务。

## 实际对照与修复

- workflows/execution_truth.py、pipeline_status_sync.py、video_pipeline.py、apps/outbox/worker_inbox.py：原实现以活跃工作流与未过期租约为证据。Rust claim 原来可以不断领取耗尽次数的过期非投稿阶段；现在限制 generation/current_stage/attempt budget，并在 Task -> Workflow -> Job 锁顺序下单次结算耗尽的当前阶段。暂停、取消、活租约不结算；publish 的未知回执仍走原独立 quarantine。迟到的失败/心跳不能复活过期租约。人工暂停后如最终尝试已耗尽且租约过期，恢复时仍按预算结算，需要显式业务重试；不自动增加预算。
- storage/filesystem.py、storage/recovery.py、subtitle_service/subtitle_finalization.py、worker.py：原成果临时文件和任务状态处理。Rust 本地媒体输出采用执行专属临时文件、完整 hash/size 校验、数据库当前执行租约及 GC 状态校验，再在持锁事务内同步 rename；拒绝已登记 key 的内容漂移。字幕、渲染、YouDub 视频接入同一保护，保留原 key 和业务表。事务回滚不能撤销文件系统 rename；未登记的完整文件仍可能遗留，后续需加强孤儿识别。工作检查点改为独立临时文件，但还不是所有工作检查点的数据库级原子 fence。不能宣称全存储写路径、所有 symlink/外部写入竞争均解决。
- apps/egress_gateway/client.py/main.py：原 16 个实际 DNS 解析槽位（超时仍持有至真实 DNS 调用结束）、保留 DNS 顺序、响应重复头 Latin-1 合并。Rust 补齐；新增 40 个并发 fetch 槽位和 10s 请求体读取上限；禁止内部 token 透传，保留更严格的 SSRF 判定、禁代理和总 deadline。实际 HTTP mock 验证重复头、Retry-After、慢重定向与慢响应不能突破总预算；未做真实公网 DNS 洪泛压测。
- subtitle_service/dictionaries.py:998–1072：原查询 min_quality clamp(0,1)、limit clamp(1,50)，Rust 旧实现拒绝越界。现在同原 Python clamp；继续使用原 SQL 排序/语言/领域/usage_count。

## 继续逐模块语义审查的已知证据

|优先级|原 Python|Rust 对照|后续|
|---|---|---|---|
|P0|subtitle_service/worker.py:1751–1800，subtitle_readability.py:81–401，subtitle_quality.py|subtitles.rs:161–188 仅计算报告，media/asr.rs 直接写 SRT/ASS|缺少真实可读性优化：中英宽度、标点断行、双语对齐、适度延长、长句拆分、质量报告。必须区分对齐用 segments 和展示用 cues，防止破坏选择性重译、YouDub。|
|P0|realtime.py:262–440、orchestrator_api/realtime.py|realtime.rs:117–155,260–291|事件轮询只 join 尚存任务及 workflow_events，事件时间不是提交顺序；普通 CRUD/删除和延迟提交可能漏通知。需原表持久事件与提交后 resync，隔离事务回滚、重连、并发提交。|
|P1|utils/resources.py、utils/cpu.py、utils/intel_gpu.py、orchestrator_api/services/system_service.py|system_resources.rs:29–75|cpu.percent 和 intel_gpu 固定 null，cgroup v1 回退缺失；应实际采样 CPU/GPU，统一 HTTP 和 resources WebSocket 并避免每个订阅者重复采样。|
|P1|subtitle_service/queue_telemetry.py、orchestrator_api/scheduler.py|subtitle_queue_v1.rs:383–435，RenderQueuePage.tsx|原 broker 已退休，Rust ready/active/awaiting 统计需与新 claim generation 和预算保持一致；页面 scheduler 类型/默认仍是 hatchet，20s broker 快照轮询需明确区分旧测试禁止的资源轮询。|
|P0|storage/recovery.py:48–133|ingest.rs、download_runtime.rs、storage.rs|旧 reset 删除历史投稿记录，不直接复制。需安全来源丢失检测、保留历史外部回执、generation 隔离与清理故障注入。|
|P1|apps/security/trusted_proxies.py、rate_limits.py、service_auth.py|auth.rs、legacy_service_v1.rs|核实原可信反代 CIDR/主机刷新、客户端 IP 与限流、bootstrap 及安全审计等差异；当前仅阅读关键 Python 实现，不声明缺失或通过。|
|P1|apps/publish_lifecycle.py、social_publisher/worker.py、account_store.py|publish_batch_events.rs、sau_worker_native_v1.rs|进一步验证账户并发、未知结果、平台凭据轮换与原批次状态衔接。|
|P1|apps/orchestrator_api/services/image_validation.py、asset_service.py|task_reliability_v1_focused_diff.py 及资产 API|已有封面格式/像素/重编码相关专项，需再次验证恶意容器、超大图片和 Range/Content-Disposition。|
|P1|db/auto_migrate.py、schema_compat.py、migrations/versions|41 Rust migrations|原库 35 表及 1523445 行恢复已通过第十批，需继续逐项映射 legacy schema compatibility 和滚动切换。|
|P2|ai、subtitle_service 剩余 provider/模型/RAG、youtube_ingest、youtube_home_feed|此前批次及各 parity 文档|按照清单补齐每个函数语义与测试证据，复用已验证实现而非重复编写。|

## 已执行的验收

- 最终 Rust lib：255 passed；env-gated 数据库测试另外真实运行，不以默认跳过充当实测。
- PostgreSQL 专项：exhausted-lease（并发结算、有效/暂停/过期/投稿、旧 token、hash、GC fence、旧执行不得覆盖现有文件、有效新成果）、translation、external-whisper、dub-handoff 均通过；YouDub 为 mock 模型加真实 HTTP/MP4，不是 GPU 模型验收。
- Egress standalone HTTP：通过原 auth、SSRF、限制、头和 deadline 断言。
- Python reference：显式加载 anyio 插件后 46 passed / 1 failed。剩余失败是原静态测试 test_render_queue_uses_realtime_updates_without_resource_polling 禁止页面任何 setInterval，而既有页面每 20s 刷新 Broker 指标；未掩盖、未更改测试或删除原监控。
- 首次 lease fixture 因测试容器 100ms idle transaction timeout 中断；仅该专项改 5s 后通过。一次编译因删除 checkpoint helper 失败，恢复独立临时文件 helper 后最终库与专项通过。
- 不操作生产库、不部署、不调用真实投稿平台。现有 Release 60d8c41 不包含第十一批代码。
- Dictionary real PostgreSQL + HTTP differential: CSV/TSV/JSONL/CEDICT/TMX/TBX presets, SHA-256 archives, source/batch/entry writes, max_entries, upsert/replace and normalized lookup, clamped bounds, ordering all passed.
