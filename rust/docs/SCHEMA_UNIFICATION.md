# VideoRoll Rust 原位替换：统一数据库事实源

目标是保留旧 Python VideoRoll 的同一个 PostgreSQL、原业务主键和已有记录。Rust 是执行引擎的替换，不创建 Python/Rust 两套 Task / Pipeline / Worker / Publish 数据库。**Rust 新版尚未投入生产；本方案后半部分依赖这一前提：先检查旧 V2 辅助表为空，否则停止迁移而不是删除其中数据。**

## 物理重复表：24/24 已处理

| 原 Rust 表 | 统一之后的物理存储 | 说明 |
| --- | --- | --- |
| `vr2_settings` | `app_settings` | 原配置主键、版本和字段 |
| `vr2_alerts` | `alert_events` | 原告警 ID、指纹、状态、details |
| `vr2_managed_tasks` | `tasks` | 原任务 ID，加运行引擎归属 |
| `vr2_task_titles` | `app_settings` | `task.title.<uuid>` |
| `vr2_workflows` | `pipeline_runs` | 原工作流 ID、状态、Rust generation 与 stage |
| `vr2_jobs` | `operation_inbox` | 原 operation_key 幂等，Rust lease、stage、fence |
| `vr2_render_workers` | `render_workers` | vrw_ 与 Rust 64 位凭据；跨协议不可顶替 |
| `vr2_render_enrollments` | `render_worker_enrollments` | 一次性注册码，历史凭据保留 |
| `vr2_render_executions` | `render_executions` | 原 `render_job_id` 与 Rust `job_id` 两种关联 |
| `vr2_dubbing_runs` | `operation_inbox` | YouDub 产物 UUID / SRT / 来源证明 |
| `vr2_bilibili_p2_appends` | `operation_inbox` | P2 独立状态 / CID / unknown，原 P1 不变 |
| `vr2_social_publish_intents` | `operation_inbox` | SAU 幂等交付，真实 SAU 结果仍在 `publish_jobs` |
| `vr2_publish_meta` | `tasks` | 单任务稿件元信息与修订号 |
| `vr2_publish_reviews` | `tasks` | 审核指纹、结果，更新配置后失效 |
| `vr2_download_progress` | `tasks` | 下载进度及当前原生 Stage Job UUID，租约防旧数据覆盖 |
| `vr2_bilibili_subtitle_syncs` | `operation_inbox` | P1/P2 字幕同步结果与独立租约、unknown 防重 |
| `vr2_knowledge_items` | `translation_knowledge_items` | 复用原知识库 UUID/title/content；Rust 作用域附加字段，pgvector 原列不覆盖 |
| `vr2_rag_agent_runs` | `pipeline_runs` | 同一工作流的研究状态、计划、证据及结果 |
| `vr2_playout_links` | `playout_asset_links` | 原旧播控关联保留，同资产可跨频道；Rust staging / detach 独立字段 |
| `vr2_objects` | `storage_objects` | **独立必要基础设施**：跨任务对象 GC 生命周期，原 `assets` 不具备 |
| `vr2_object_refs` | `storage_object_refs` | **独立必要基础设施**：显式多所有者引用和 GC 防误删 |
| `vr2_workdirs` | `storage_workdirs` | **独立必要基础设施**：带租约的工作目录垃圾回收 |
| `vr2_events` | `workflow_events` | **独立必要基础设施**：工作流事件，与原 `outbox_events` 可靠投递语义不同 |
| `vr2_login_limits` | `auth_login_limits` | **独立必要基础设施**：原 `security_audit_events` 是审计，不能提供原子限流 |

原版不具备相同语义的 5 种辅助状态必须保留为公共支持表；它们不是另一份 `tasks/assets/publish_jobs` 业务复制表。**全新隔离库检查为 0 张 `vr2_*` 物理表**。20 个同名 `vr2_*` 兼容视图没有独立存储，供原生查询适配逐步清理；另 4 个影子表名称直接取消。

## 迁移与测试边界

- 前向迁移文件 `20261123000019`～`20261123000033` 必须按顺序执行；原生产数据库本轮没有执行任何 DDL。
- `20261123000019`～`20261123000028` 做严格非冲突历史值合并及跨版本 ID/FK/Lease 校验。
- `20261123000029` 把 5 张原版没有等价结构的辅助表规范命名，原外键和记录保留。
- `20261123000030`～`20261123000033` 因 Rust 尚无生产数据而采用 **空影子表前置断言**。只有旧表真正为空才允许移除，避免不知情清除历史数据。
- 当前新增验证脚本 `rust/tests/schema_unify_empty_v2.py` 对全套原版夹具运行所有迁移，检查旧 Python Task 不变、24 张影子物理表清零、元数据/审核/知识/RAG/字幕/播控单表读写。
- 新 Rust 存储与多阶段回调仍必须通过 `cargo check`、`cargo test`、`smoke-isolated.sh`，测试绝不提交真实外部平台稿件。
- 生产切换需要先停 Python 作业/调度器、冻结正在执行的提交及租约、备份 DB 和媒体、核验旧 V2 表为空、事务化执行迁移、Rust 灰度检查、失败回滚。**不能边运行 Python 生产调度边修改表。**

数据库表统一进度 24/24 不代表原版 217 个 API 全部迁移：截至此次审计为 **147/217 部分原生 V1 路由，0/217 完整生产等价签收**。剩余接口和真实环境投稿故障注入必须另行完成，不能直接称 Rust 已具备全面投产条件。
