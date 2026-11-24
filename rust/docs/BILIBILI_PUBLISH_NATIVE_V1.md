# B 站直接投稿：从原 Python 合约移植的 Rust 原生部分适配

2026-11-24：迁移原 `src/videoroll/apps/bilibili_publisher/main.py:publish` 与 `schemas.py:PublishRequest` 的 `POST /bilibili/publish` 接口。Rust 只部分兼容旧业务语义，**不能据路由存在推断为生产完整等价**。

## 复用原有业务表、字段和接口

- 直接读取原版 `tasks`、`assets`、`app_settings['bilibili.auth']`、`app_settings['publish.platforms']`，以 `publish_jobs` 作为旧版前端及 Python 可读取的投稿作业主记录。返回原 `PublishResponse` 字段 `job_id/state/aid/bvid/response`。原 `GET /bilibili/publish/jobs/{job_id}` 不需要改动。
- Rust 通过原 `pipeline_runs` 和原 `operation_inbox`（已有兼容只读/可更新 VIEW `vr2_workflows`、`vr2_jobs`）创建持久化 Publish stage，**不再交给 Celery/Hatchet**。使用 Python 原 `publish-job:{job_id}` 幂等键，在同一事务插入原 `publish_jobs(state=submitting)` 及 Python 恢复防护行 `operation_inbox(status=processing)`，防止旧 Python 发布调度器误判该任务无人处理。
- `tasks` 使用原 `publishing/published` 状态，B 站 UPOS/Archive 真实确认后**在 Rust Publish 事务中同步**写入原 `publish_jobs(state=published, aid, bvid, external_id, external_url, response_json)`；原 Python Inbox 状态变为 `completed`。网络结果不确定或超时后 Rust Publish 状态为 `unknown`，原 `publish_jobs` 也变成 `unknown`，且与 Python 旧版相同将 `tasks.status` 写为 `failed`、`error_code` 写为 `PUBLISH_OUTCOME_UNKNOWN`；标记需要外部平台核验，禁止对同一请求自动重投。
- 用户再次提交时，在同一个 `tasks FOR UPDATE` 排它锁下先检索原 `publish_jobs` 成功、进行中和未知回执并复用，且拒绝新的独立 Publish stage；不依赖 Redis 内存锁。对其他活跃工作流、已经投递的 Publish stage 做跨代际防重复保护。

## 复用原 Python 请求选项与实际投稿执行

- 已支持 `video`、`video_parts`、`cover` 的旧版 `InputRef`，校验当前 Rust 管理的 `v2/` 资源实际存在并归任务所有。单 P 要求 `video_final`，双 P 额外要求 `video_dubbed` 和成功 YouDub 的原视频及字幕来源一致。缺失配音证明、跨任务资源、相同的 P1/P2 键一律拒绝。双 P 必须以**一次 Archive POST** 提交恰好两个有序视频分 P。
- 沿用原 Python `meta` 字段与别名，包含 `typeid/tid`、`tags/tag/keywords`、`copyright` 默认 1、简介、来源、话题与活动 ID、评论/弹幕等 Web 投稿字段。复用原生 `ArchiveMeta.validate`、原 AI 投稿审核与原活动 ID 验证，禁止静默忽略未知字段。
- `typeid_mode='meta'` 直接使用已验证的 `typeid`；`bilibili_predict`（原版默认）使用已上传的 P1 `filename/upload_id/title` 向原 B 站分区预测接口查询，失败不伪造推荐 ID，而继续使用请求中可信的 `typeid`。`ai_summary` 复用现有 `POST /bilibili/archive/type/recommend` 的同一份 Rust 内部实现（任务摘要优先、原翻译模型配置、原模型请求及 AI 用量记录、B 站实时叶子分区白名单），若 AI 不可用或返回伪造的分区 ID，则依 Python 旧版顺序回退到 B 站 `bilibili_predict`，最终使用已验证的原 `typeid`。
- 复用 Rust 现有 `BilibiliClient` UPOS 分片上传、受约束的 CDN 域名验证、分片重试、单次创建 Archive 的不可逆边界。封面从任务拥有的 `cover_image` 上传到原 B 站封面接口，返回实际 HTTPS 封面 URL 写入一次性投稿请求。
- 所有生产外部服务仍只能使用官方 HTTPS，UPOS 不附带 B 站 Cookie；仅在明确启用的单机隔离测试条件下允许与配置中相同的 127.0.0.1 HTTP Creator/UPOS 目标。

## 暂不支持的旧合约

- `account_id` 已接通原 `accounts(platform=bilibili)` 账号行。Rust 解析原 `secrets_encrypted` 的 `dbplain:v1:` 格式及兼容 JSON Cookie 字段，**绝不**把指定账号降级成全局 `bilibili.auth`。入队时同时保存原 `publish_jobs.account_id/bili_account_id`、工作流账号 ID 和 Cookie+`rotated_at` 指纹；Worker 开始上传前再次读取同一账号记录并校验 ID/指纹，发现停用、平台不匹配、丢失或轮换凭据，直接进入 `unknown` 且不得重投。`batch_id` 必须显式包含该 B 站账号目标，允许同一任务两个 B 站账号各产生独立稿件，并在全部目标完成后才结算批次。
- 由于旧 Python Worker 会无条件读取全局 Cookie，旧版 `account_id` 虽然有字段却会错号投稿；Rust **没有复制这个缺陷**。新增 Rust API 扩展：`GET /bilibili/accounts`、`POST /bilibili/accounts`（按名称 upsert/重置账号检查状态）、`DELETE /bilibili/accounts/{id}`（停用并清空 Cookie）、`POST /bilibili/accounts/{id}/check`（使用该账号 Cookie 向官方 B 站成员接口核对 UID）。账号 API 返回 ID、名称、状态/UID 等必要资料，**不返回 Cookie**，亦不向客户端回显原始凭据。此 CRUD 是 Rust 扩展而非原 Python 217 条路由之一。
- 保持与 Python 现有数据库恢复模型相同的 Cookie 存储方式（PostgreSQL 可恢复 `dbplain:v1:`）；数据库权限和备份必须按高敏感凭据保护。Fernet 历史密文仍要求先在可信旧环境解密/迁移，Rust 不会猜测原旧密钥。默认账号仍读原全局 `bilibili.auth`，且不会被命名账号的更新修改。

- `batch_id` 已支持原 `publish_batches.expected_targets` 中包含 `bilibili:default` 的多目标批次，也支持 B 站和其他平台各自拥有独立活动批次。全部复用原表 `publish_batches`、`publish_jobs`、`outbox_events` 与原字段。锁定 Task→Workflow→Job→Batch；Rust 的单次 B 站回执和批次状态在同一个事务中结算。按 Python `publish_lifecycle.py` 对每个 `(platform,account_id)` 选取最新作业，缺少作业时回退原 `outcomes_json`：全部成功才标 `succeeded`，还有未完成目标则维持 `active`，部分失败为 `partial_failed`，全部失败为 `failed`。任务状态按照各渠道的**最新批次**综合计算，防止 B 站成功提前覆盖仍在运行的抖音等目标。
- 批次终态生成与原 Python 相同 operation_key 的 `workflow.publish.finished` Outbox；所有当前渠道均成功时生成延迟 24 小时的 `publish.cleanup` Outbox（只入队不伪称实际执行）。原 `PublishResponse`、原作业租约与未知回执的防重原则不变；历史空 `expected_targets=[]` 的遗留 marker 不参与当前批次统计。已有批次终态的重复请求复用其原回执，不会重新发布或把 B 站成功回执误转移给新批次。
- `force_retry=true` 会覆盖未知平台回执，Rust 直接拒绝，必须人工检查 B 站真实稿件后再设计有审核的恢复流程。
- `ai_summary` 已接通独立 Rust 分区推荐器的同源代码路径，但仍缺 Python 原版更丰富的审计记录、分区结果解释、动态活动与合集后处理的全量差分；不能据此认为整条直投链路已完成生产验收。
- 当前仅在 B 站直投的原生 Rust 结算点执行聚合；外部渠道**之后**单独完成时，应由该渠道的原 Python `reconcile_publish_batch` 或后续 Rust 社交平台结算器更新。原 Outbox 消费者、真实多平台同步和可靠执行 24 小时清理仍需集成验收。
- 原 Python 完整上传速度、动态活动匹配、视频合集归档、投稿后所有审计和错误映射、复杂上传恢复并未完成逐条对照。直接单 P 投稿也仅接受经验证、实际归属 `video_final` 的 Rust 资产。

## 隔离验证

`rust/tests/bilibili_publish_direct_v1_isolated.py` 与 `rust/tests/mock_bilibili_publish_direct.py` 使用临时 PostgreSQL + 本地模拟 B 站 + 本地 OpenAI 兼容接口 + 真实 Rust HTTP/Worker。已验证：拒绝不安全的 Python 旧请求选项；对同一任务的多次 HTTP 请求只创建一个原作业/一个 Rust Publish 阶段；UPOS 分片上传；B 站真实格式分区预测；封面上传；单 P 投稿只发送一次 Archive POST；已投出稿件的原表回执及 GET 读取；模拟不可确定的 HTTP 503 时原作业和 Rust 任务进入 `unknown`、绝不重复投稿；双 P 前必须有任务同源、当前 SRT 对应的成功 YouDub 记录，且双 P 以两个有序 CID 一次提交；AI 分类器使用任务历史摘要和原 Provider 配置，返回 B 站确实存在的叶子分区或回退到分区预测；仅含 B 站默认账号的原批次成功、未知结果和重复请求均更新或复用原 `publish_batches`。已覆盖多目标下 B 站先完成而抖音仍等待、其他平台已成功/失败、单独渠道批次并行、旧空批次标记、24 小时 Outbox 清理排期、跨批次幂等、防止误标任务成功；还模拟 Rust Worker 租约过期和重启，保证不会重复 Archive 提交。

测试使用一次性 Cookie、隔离数据库及本地 FFmpeg 视频，不触碰生产账号、生产 Python/Hatchet 或实际 B 站；仍需真实平台与长时间断点故障注入的生产等价验收。

## 2026-11-24 多目标批次、旧表和 Worker 恢复增量

新增 `rust/src/bilibili_batch_reconcile_v1.rs`，只使用原 `publish_batches` / `publish_jobs` / `outbox_events`，不创建新业务表。临时数据库 + 本地 B 站模拟 + 真实 Rust Worker 验证 `active/succeeded/partial_failed/failed`、跨平台独立批次、历史空目标列表、Outbox 延迟清理以及 B 站 HTTP 503 `unknown`。租约过期扫描从一次性先锁所有作业改为限量 100 条、逐项 Task→Workflow→Job→Batch 顺序锁定；取消已经开始的投稿也同步隔离原 `publish_jobs`，不依赖假设外部平台回绝。端到端验证只覆盖模拟环境，尚未声称真实 B 站和所有社交平台的生产等价。
