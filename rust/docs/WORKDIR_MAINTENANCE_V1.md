# Rust V1 工作目录维护兼容实现（2026-11-24）

新增 GET /maintenance/workdir 和 POST /maintenance/workdir/cleanup，两条都是原 Python REST 路径。

扫描读取原 subtitle_jobs、render_jobs、tasks、operation_inbox，并按旧版的 900 秒宽限期、作业状态与目录大小输出 DTO；同时保守保护 Rust 原生正在运行、暂停及回执不明的 workflow。跳过符号链接，扫描失败时拒绝清理。

工作目录从 VIDEOROLL_V2_WORK_DIR 或 WORK_DIR 获取；默认 /tmp/videoroll。与旧版共用 app_settings 的 orchestrator.workdir_maintenance 锁，不会并行执行两个版本的工作目录维护。

POST 默认关闭，只有显式设置 VIDEOROLL_V2_ALLOW_V1_WORKDIR_CLEANUP=true 才执行删除。每次至多删除 50 个可回收目录；删除前会单独重新扫描候选目录、复核数据库任务状态及目录时间/大小，并且不会递归复查其他目录。实际返回删除数、路径和错误，不伪造回收量。

验证：使用临时 PostgreSQL、受限临时工作目录和 Python 原版 scan_workdir 进行差分；覆盖正常完成、活动作业、15 分钟宽限、跨进程锁冲突、符号链接以及重复清理。未触碰生产数据或生产服务。

限制：旧版 POST /maintenance/storage/cleanup-terminal 尚未迁移。此处不执行原业务对象/资产清理。原 Python/Rust 端到端生产验收仍待完成。
