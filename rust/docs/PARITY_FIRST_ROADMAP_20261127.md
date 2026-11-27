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

## 第三十批：SAU 执行边界及原始账本恢复

原 Python 账户锁、凭据物化、Job/Inbox 租约与 started_at 执行边界已逐段对照。Rust 原生 Worker 增加启动前 Task generation、Account credential version、三账本身份及租约复核，超时恢复需三账本同 owner 且均过期；保留已有响应与进度，拒绝覆盖外部标识。当前仅全局串行，不提前放开跨实例账户并发；原生账本冲突仍需显式核实。详细边界见 SAU_WORKER_NATIVE_V1.md。专项使用无网络模拟 CLI、隔离 PG、领取时账户版本变化和 Inbox 延迟过期故障注入。

## 第三十一批：SAU 异常投递隔离

基线 a87a7ac，提交 58d35a3。原队列资格与 retry_after 保护、终态 Outbox 补偿和审计已恢复，详见 SAU_WORKER_NATIVE_V1.md。仍不能自动释放未决外部执行。

## 第三十二批：原版批量恢复最近失败任务

基线 58d35a3。参考 Python orchestrator_api/services/subtitle_service.py:271-319、630-715、tests/test_resume_failed_recent.py。原版先查询失败任务，再跳过活动字幕/渲染及未知授权；旧字幕请求复用、无字幕作业的 YouTube 任务重新入队。Rust 原入口内连接原生 job，漏掉无原生工作流的历史任务。

- 原 POST /tasks/actions/resume_failed_recent 保留 window_hours/limit 和 matched/resumed/skipped/failed 计数，Rust 所有权下 LEFT JOIN 最新原生 job；已失败可逆原生阶段按原逻辑恢复；历史无原生记录时使用最新原字幕请求、原源视频 digest/size、已有字幕快照；无字幕作业的 YouTube 任务通过 URL 校验后，源视频存在则复用，否则排队下载；本地无字幕历史跳过。原作业/资产/投稿记录不重写。预条件拒绝分类为 skipped，内部错误分类为 error。
- bulk 专用 admission 在 Task 行锁内重核失败状态、updated_at、授权、暂停、所有权、最新 native job/阶段/失败状态/租约、旧请求和 raw key；原物理账本 NOWAIT 表锁及 ownership/cleanup blockers，防止重复工作、活动历史作业、未决投稿。旧业务记录不自动接管 Python owner；历史分支拒绝任何投稿历史及已有空壳原生工作流。并发列表的过时决策可跳过而非重复创建代次。
- 安全差异：历史恢复使用原版请求的已支持字段或显式 runtime_profile，关闭自动投稿/配音，不沿用旧 after_render 副作用；原生失败阶段仍继承既有原生配置。旧输入与当前 raw 不一致、丢失文件、没有校验信息、旧选择性重译、无法证明的字幕成果及未支持请求字段保守跳过。尚未等价实现原版替换缺失输入、最新自动投稿策略、完整 legacy request 适配与所有 native stage checkpoint 复用；本轮只验证入队及账本不变，未运行新入队的全链路真实模型/平台。全局 NOWAIT 锁与逐任务文件校验有生产负载成本，需后续压测。

专项：VIDEOROLL_V2_TEST_BULK_RESUME_ONLY=true bash rust/scripts/smoke-isolated.sh；真实 PG/API、FFmpeg 生成隔离媒体、15 个命中（4 入队/11 跳过）、排除 Python/过期、重复/并发请求、原始资产及业务表对照。下一批检查全部停止/恢复及暂停的活动历史作业保护。

## 第三十三批：原版任务停止/恢复

基线 402e88d。对照 Python task_service.py:160-257、routers/tasks.py:96-139、tests/test_task_stop_controls.py。原版批量停止包括 FAILED 且字幕/渲染活跃的任务，恢复时还重新启动自动 YouTube 和 queued subtitle。Rust 之前漏掉失败活跃任务，批量逐个提交可出现前面已修改而请求整体失败；单任务有终态历史工作流时停止后无法恢复。

- bulk 统一在原 Task 行锁顺序、原物理账本 NOWAIT 表锁下处理；增加原 subtitle_jobs/render_jobs 活跃条件及等价的原生可逆阶段。单任务停止 FAILED 仍拒绝，与原契约相同。仅 Rust owner，最多 1000；保留原 matched_count/changed_count；不可安全控制的任务不计 changed，其他合法任务一起提交，SQL 错误整体回滚。
- 只暂停唯一最新的原生工作流且存在当前可逆 job，复用 remote render stop 函数保留租约/尝试/成果；已终态历史工作流不复活。存在原生/历史未决投稿、P2、活动批次、unknown workflow、多活动/旧代次冲突则保守拒绝。
- 恢复已暂停的原生代次，不新建 job；FAILED 仍有当前可逆阶段时恢复真实 checkpoint（download=CREATED/subtitle=DOWNLOADED/render=SUBTITLE_READY/dubbing=RENDERED），避免远程渲染排斥 FAILED。无活动原生工作流且所有历史账本已平静则直接恢复保存状态，即使存在终态历史工作流。只恢复状态，不自动重启旧 Hatchet/queued subtitle；未决历史作业继续保持暂停，需显式核实，不声称实际停止外部 Python 进程。现有暂停资源保护不改变。
- 限制：批量最多 1000，事务持有多个 Task 行锁及原账本表锁，繁忙时 409 需重试；不保证立即停止已经运行的外部程序；错误原因仅服务日志，原 API 仍只有计数；旧自动 YouTube 无活动原生工作流的重新入队、动态配置仍待后续。测试只用隔离数据库，不部署。
- 专项 VIDEOROLL_V2_TEST_TASK_CONTROLS_ONLY=true；还需既有远程渲染暂停回归。

## 第三十四批：自动配置按执行阶段读取，配音独立于投稿

基线 6238f40。对照 Python youtube_service.py:240-307、video_pipeline.py、publishing_service.py:692-735。V1 自动入口与 Remote API 的旧 auto_publish 参数仅保留 wire/hash 契约，不再冻结 false 或 B站；首页及频道自动扫描、明确的旧版自动启动使用 legacy_auto_runtime_profile 标记，ASR 模型与渲染默认按当前 profile；直接 V2 请求及手动参数不变。授权 proof 与新 Task/工作流同事务。已有工作流不改写；历史恢复明确禁用自动投稿/配音的围栏保留。

用户纠正原 Python 设计：自动配音只看 auto_dub_p2，不依赖 auto_publish 或 B站目标。保持 Rust 原语义，统一旧 auto_dubbing 接口与任务详情显示、设置文字；允许原 profile PUT 单独更新 auto_dub_p2 和前缀。手动 dubbing.enabled 优先，保留无语音、暂停、成果复用等原条件。配音成果仍可用于后续 B站双 P，不强制投稿。未开放其他尚未完整验收的 profile 字段，旧前端全表单保存仍需后续适配；旧运行中的冻结配置不批量改写。测试只用隔离库与实际阶段配置读取，无真实平台或 GPU 执行。

## 第三十五批：ASR/翻译配置入口收尾

基线 320a55d。对照 Python auto_profile_store.py:get_auto_profile/update_auto_profile、schemas.py:245-335、translation_checkpoint.py、translation_stage.py 与 Rust media/asr.rs:settings/runtime_asr_config、youtube_subtitles.rs。Rust 已支持实际阶段读取 ASR engine/model、摘要开关和三种 YouTube 字幕策略，原 profile PUT 却拒绝这些字段。本批开放已支持字段，校验类型/长度/枚举，模型空字符串清除覆盖，显式字幕模式优先、兼容布尔同步，旧仅布尔记录读取一致。设置首写采用与通用 settings 一致的 advisory lock 及原表冲突安全插入，避免并发首写覆盖；无新表。前端基于已加载表单快照仅发送实际改动，不丢弃未知改动或绕过未实现字段的拒绝。仍不开放尚未完全验收的格式/渲染/投稿等字段。配音和投稿保持独立。原 RAG/Agent/checkpoint 全行为仍需专项复查，本批不是完整迁移签收。

## 第三十五批续：翻译检查点提交围栏

基线 15891c4。对照 Python translation_checkpoint.py 的 source_segments_key/provider/时间戳匹配、translation_stage.py 的 batch checkpoint 与 Rust media/asr.rs 的批次 fingerprint、partial rows 和滚动上下文。原 Rust 已支持更细的缺失片段恢复，不重写。发现 stable checkpoint 在 cheap lease 检查后、临时文件写入和 rename 前存在旧租约覆盖窗口。四种检查点（YouTube 字幕、ASR 分段、翻译部分结果、翻译完整批次）统一在临时文件写完后，按 Task → Workflow → Job 顺序加锁，校验所有权/暂停/状态/代次/阶段/身份/token，并在全部锁取得后以 clock_timestamp 复查 lease，持锁完成 rename。保留原路径、格式、fingerprint 与恢复逻辑。文件 rename 与 DB commit 不具备跨系统原子性，且未覆盖临时音频文件/所有 Agent checkpoint 写入；不宣称完整消除所有文件竞争。测试：隔离 PostgreSQL+模拟 HTTP translation recovery，旧 token、暂停、停止、过期均不得覆盖新结果；完整 translation_context 集成回归。复查发现旧 partial 在模型响应缺失行时先学习摘要/上下文，可能污染恢复。改为只学习完整子批次响应的上下文，仍保留合法已返回行及针对缺失行修复；旧 revision=1 部分检查点失效重算，不改既有完整批次。原集成 fixture 补齐新增的缺失行修复请求并故意使之失败以继续验证持久恢复，仍核查失败摘要/上下文不传播。

## 第三十五批续：RAG 研究写入围栏

基线 6db88cf。对照 Python agent_runtime.py:check_budget/before_llm/before_tool 与 Rust agent.rs/agent_parallel.rs。现有并行 Agent 已持久化父子研究、预算预约及工具收据，单 Agent 只有逐次 lease 检查，门控、进度、最终结果存在检查和写入之间竞争。统一使用原 Task → Pipeline → Inbox 行锁，核对 Rust 所有权、暂停/取消、当前阶段/代次、精确作业身份及 token，取得最后一把锁后再以 clock_timestamp 核对租约；串行门控、证据、最终结果、失败与缓存读取进入同一围栏，匹配原研究 ID/工作流/任务，拒绝影响其他研究。外部请求不持有事务。仍不改变串行模型/工具预算跨重试重置和未完成步骤不复用的行为；并行预算沿用既有持久化计数，未新增 token/成本预算；外部请求已经发出时无法回收。通过实际迁移 PG 测试旧 token、错误代次/阶段、暂停、所有权、锁等待期间过期及正常串行/并行研究回归。

## 第三十五批续：串行 Agent 持久化预算和步骤收据

基线 dbb1d54。参考 Python agent_runtime.py 的 before_llm/before_tool/parent_runtime 预算及 Rust agent_parallel.rs:reserve/receipt。串行同原研究 fingerprint 在 pipeline_runs.research_plan.serial_checkpoint (revision=1) 保存门控结果、已完成工具步、有限对话、知识/网页证据、合法待执行动作及累计预约计数。外部请求前在统一任务租约围栏事务中持久化预约，失败/未知请求不退款；有效模型动作先保存，成功工具收据和进度同事务提交。每次重试复用原门控和已完成工具步；缺少成功收据的调用可能再次发出但消耗剩余预算。逻辑步骤仍按 max_research_steps，模型请求上限 max_research_steps + 门控1（如开启）+ 全局共享额外尝试1；知识检索预约上限 max_research_steps+1，网页预约严格 max_web_searches；没有改变并行预算。对话45KB、动作32KB、检查点256KiB边界；非法/重复/非 grounded 动作不保存为待执行。旧未完成研究无该字段时从头开始；原成功结果缓存仍保留；新指纹不复用旧状态。本轮计数是逻辑模型/工具调用预约，不是 HTTP 实际次数、token、费用或跨重试总耗时；没有补齐所有 Provider 内部重试及最终术语冲突检查的预算。动态检索证据按同指纹原快照恢复，知识撤销/原检索策略变化未额外校验，未来需细化。测试 VIDEOROLL_V2_TEST_TRANSLATION_CONTEXT_ONLY=true VIDEOROLL_TEST_SERIAL_RAG_FAULT=true VIDEOROLL_V2_TEST_RESEARCH_FENCE=true：检索成功后模型503，真实 Worker 重试仅恢复缺失结果，门控1次、检索1次、模型预约4次，无真实外部服务。

## 第三十五批收尾：自动投稿与编码设置

基线 1c2be8d。原 Python auto_profile_store.py 的 update_auto_profile、processing.py 的 CPU SVT-AV1 参数与 Rust subtitle_management_v1、publish_legacy、task_manual、media/ffmpeg 对照。开放 auto_publish/auto_publish_platforms、burn_in/video_codec/use_intel_gpu/video_preset/video_crf 的原 PUT；平台白名单和去重沿用实际投稿选择函数，配音独立、授权和未知回执保护不变。仅接受 H.264 预设名称、AV1 数字 0..13；切换编码清除不兼容旧预设；显式 null 可恢复质量/预设默认值；非法输入原子拒绝。修复 CPU AV1 preset 固定8及 CRF 沿用 H.264 1..51 的问题，显式 AV1 CRF 0..63、H.264 0..51，保留 Rust 原未指定预设默认8、质量默认23，而非照搬 Python AV1 4/24。VAAPI 已有质量映射；use_intel_gpu=false 仍遵循既有环境 GPU auto 设置，并保留 CPU 回退，不是强制禁用显卡。格式/软字幕/ASS样式/字体缩放/未完整接通的投稿设置仍拒绝写入，不称完整设置迁移完成。隔离 HTTP 配置保存后，由已存在工作流读取最新配置；核对实际 FFmpeg 参数，不真实投稿或运行 GPU。测试：AUTO_RUNTIME_PROFILE_ONLY（真实隔离 PG/HTTP、阶段配置实际消费、268 项库测试）与 DIFF_ONLY 均退出 0。差分初次在既有历史 P2 夹具投影及不同调度器默认并发处失败；仅对特定 Rust-owned 无当前 native job 的旧夹具断言不进入可执行队列，并分别检查 Rust worker_count 与 Python Hatchet slots，保留其余队列逐项比较，未更改队列实现。根分区 target 约 4.4GiB，/4t 有约 3TiB 可用，当前尚未迁移构建目录。
