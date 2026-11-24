# 任务 B：工作流兼容性核查记录

基线：66513da；独立分支：codex/rust-workflow-compat。此文档仅记录已核实证据，不代表接口验收。

## 已完成的安全修正

- `rust/src/storage.rs::collect_workdirs` 在物理删除工作目录前，除检查工作流状态、storage_objects、assets、subtitles 和终态任务外，增加 `operation_inbox` 活跃操作保护。
- 活跃状态：`pending`、`awaiting`、`running`、`processing`；存在对应任务记录时延后删除。避免 Python/Rust 共用旧任务表时任务终态先于实际 worker 结束造成误删。
- 旧路由扫描器已解析导入常量；`GET/POST None` 对应 `/remote/auto/youtube`，不是合法的 None 路径。

## 尚未完成/风险

- `collect_workdirs` 仅处理 `v2/{raw,sub,render,dubbing}/{task_id}`，不能等价替代旧 `/maintenance/workdir/cleanup` 的全目录扫描。
- Rust scheduler 仍引用 `vr2_workflows`/`vr2_jobs`，须先统一持久化语义再做调度和恢复验收。
- 当前开发机 `cargo fmt` 缺组件，离线 `cargo check` 缺 `anyhow` 索引；不能声明编译通过。
- 本次没有运行临时 PostgreSQL 或生产测试，未启动任何清理动作。

## 本轮新增：知识库批量导入

- 新增原生 `POST /subtitle/knowledge/import`，支持 multipart JSON、CSV、SRT，以及 5 MiB 文件、2000 条记录上限。
- SRT 以最多 50 条字幕/4000 字符分块写入原 `translation_knowledge_items`；term 去重与 document Embedding 均复用现有 Rust 原生 upsert。
- 单条失败按原版契约计数、保留前 50 条错误，不伪造未写入的记录；需要显式打开 `VIDEOROLL_V2_ALLOW_V1_SUBTITLE_SETTINGS_WRITES`。
- `cargo check --locked --offline`、141 项 Rust 全量测试、176/217 契约矩阵静态检查均通过。临时 PostgreSQL + Mock Embedding 隔离差分通过（JSON/CSV/SRT 导入、术语去重、跳过无效项及真实原业务表向量持久化）；生产配置及真实模型仍未验证，不能视为全面等价。
