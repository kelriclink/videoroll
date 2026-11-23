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
