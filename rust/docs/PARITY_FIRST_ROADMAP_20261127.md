# 原版功能优先与开源优化验收路线（第二十八批）

基线 d0b9fd7。用户要求先完成原 Python 功能，再应用上一批开源优化。保持原 217 API、原 PostgreSQL 业务表、原媒体路径/任务 ID，以及 SAU、YouDub、ffplayout 等第三方依赖边界；不照搬危险副作用。先按 Python 源码、原测试、真实隔离 HTTP/PG/媒体对照验收，再独立优化及回归。未通过生产配置/真实平台/全量差分，不宣称完全兼容。

|编号|原 Python 功能/缺口先验收|后续开源优化|状态|
|---|---|---|---|
|1|独立投稿状态恢复、已成功回执、按目标重试；batch currentness|Postiz/Temporal：UNKNOWN 外部证据、独立目标人工核实、账号级幂等|已有聚合与单批次保守恢复；独立确认、多批次异常未验收|
|2|SAU 导入、登录、检测、凭据轮换、投稿|pg-boss 分组并发、credential fence|接口/原生执行已有；跨进程、旧凭据测试待补|
|3|FileStore.cleanup_partials、旧 pending delete/终态清理|Immich 只读完整性、tus/yt-dlp 临时文件身份与断点保护|本批补回 flat sibling 临时文件处理、注册资产完整性 CLI/浅目录孤儿候选|
|4|调度优先级、等待统计、暂停/恢复及幂等|Graphile Worker 事件唤醒、pg-boss 退避/死信、持久化扫描游标|已有单飞扫描与部分遥测；持久化 due、吞吐及细粒度锁待验证|
|5|原 ASR/翻译/RAG、字幕可读性、独立重译与时间轴|VideoLingo 真实字体排版、WhisperX 词级/说话人质量评测|原生实现和专项已有；完整差分和真实样本待补|
|6|原 YouDub 配音/音频交接、P2 渲染|pyVideoTrans 配音后二次识别、时间轴一致性、输入签名缓存|已有交接与 P2 测试；真实 GPU 模型和质量待验收|
|7|原 provider 并发、超时/失败退避|pg-boss/Temporal 重试预算、分类熔断与监控|已有 ASR/RAG gate；多 provider 压测待补|
|8|原管理员初始化状态/API、安全审计、原 UI 操作|安全契约差分、低开销监控|有安全修复，仍有原初始化 HTTP 差异|
|9|完整生产配置、历史所有权及 217 API 回归|故障注入、容量基线及切换门禁|历史快照恢复已有；最新版未全量签收|

## 第二十八批：存储原功能及优化

原 Python storage/filesystem.py 的 FileStore.cleanup_partials 扫描 STORAGE_ROOT 父目录下 .partial，默认 86400 秒，最少 60 秒。upload_file/put_bytes/copy_object 使用 .<destination>.<8 字符>.partial；download_file 使用目标旁的 PID 临时文件。Python 不持 flock，copy_object 可能保留旧 mtime 或创建硬链接。不能在并行运行 Python 时照搬原定时清理。

- legacy-partials 默认只读报告；仅处理 flat sibling .partial，识别 Python tempfile 名字。显式 --delete 精确文件名、--quiesced、--exclusive-root 才执行离线删除，操作者必须实际停止所有写入该共享目录的进程并确认归属。不自动打开定时清理，不遍历子目录，不删除不匹配名称、活动锁、近期、硬链接、符号链接。目录 FD + openat O_NOFOLLOW/O_PATH、inode/ctime/mtime/nlink 复核，unlinkat。扫描最多 1000 条原始目录项，truncated 时需进一步人工排查；删除最多 1000 个明确名字。保守限定原实际 flat upload 形态，不宣称递归清理完全等价。quiesced/exclusive-root 是人工断言而非程序证明。
- storage-integrity-audit：原 storage_objects、assets、subtitles 三表键并集，读事务、C collation 分页、最多 500 键/页；记录未登记文件候选、缺失、大小/校验和差异、冲突元数据、无校验和、不可读/非普通文件、非 live registry；hash 默认 256 MiB、最高 10 GiB 单页预算，0 禁用非空文件 hash。仅按指定单层 --directory 查看普通文件，不递归、不检查所有外部引用，不把候选当作可删除。排除 .part/.partial 与原 pending-delete、已登记引用；文件系统与 DB 不是原子快照。描述符相对路径访问，拒绝中间 symlink，检查 hash 前后 inode/size/mtime/ctime。报告 CLI JSON，尚无前端页面、定时任务、完整文件系统遍历或自动修复。
- SQL 每条巡检语句设 15 秒超时，文件系统遍历/哈希不在数据库事务内；C collation 和键并集仍可能需要排序/扫描原表，页数不等于索引命中，不声明生产规模性能。游标不作为文件路径使用，非法键也可继续分页。
- 无生产表、API 或新增依赖，实际测试仅使用临时 PG 和独立根，禁止生产清理。原版存储删除及其他维护任务不改变。后续增加目录游标、在途成果/播放路径等更全面引用证据、实际负载及前端展示。

测试：VIDEOROLL_V2_TEST_STORAGE_INTEGRITY_ONLY=true bash rust/scripts/smoke-isolated.sh。

## 第二十九批：原版独立已投稿状态补偿

基线 3142e93。原 publishing_service.reconcile_published_task_state 及 task_service.list_tasks/get_task 在读取时补偿 batch_id=NULL 的 published job。原版先按状态筛选/限制页，再补偿并移除不再匹配筛选条件的行。Rust 原 V1 list/detail 未实现。现在恢复这一行为，仅在原两个 GET 入口触发（内部 read_task 继续纯读），保留原字段/状态筛选。

安全差异：仅 Rust 所有权、无取消/暂停/旧任务锁、无任何历史批次、全部独立投稿记录已 published 且有明确平台标识、不含未核实标记；无活动工作流/阶段/投递/租约/P2/SAU intent、无比回执更新的工作流。其他失败、未知或冲突目标留待人工核对，不根据任意旧成功记录覆盖现有状态。Task 行锁 SKIP LOCKED + 原物理表 NOWAIT 锁排除并发写者，争用时读原状态而不等待。仅更新 tasks.status/error_code/error_message/updated_at，复用原事务实时通知，不改变回执、工作流、清理计划，不发外部请求。

SAU 代码检查：当前原生领取仍采用全局单投稿互斥；账户导入/删除/检查使用 account_busy 阻止 submitting/checking，登录和检查完成以 rotated_at/加密凭据等比较保护旧版本写回。尚未引入分组并发。需要进一步验证多进程的浏览器目录隔离及执行边界凭据复核，避免仅放开全局锁导致同一账号或浏览器状态竞争。

测试：VIDEOROLL_V2_TEST_LEGACY_PUBLISH_RECONCILE_ONLY=true bash rust/scripts/smoke-isolated.sh。
