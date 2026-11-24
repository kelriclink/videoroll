# B 站批次完成通知与资源清理 Rust 原生化

原 B 站批次结算继续使用 publish_batches、publish_jobs 与 tasks，并保持
多账号、多平台当前批次的聚合规则。Rust 不再生成新的
subtitle_service.push_publish_workflow_event 或 subtitle_service.cleanup_task
Celery 任务。原 outbox_events 的业务事件类型、aggregate_id、operation_key
保持不变，新执行 handler 为 rust.publish.batch_finished / rust.publish.cleanup，
状态 native_pending / native_completed，旧 Python Dispatcher 不会领取。
operation_inbox 使用 runtime_engine=rust-publish-batch。无新增业务表。

## 消费与通知

Worker 每 5 秒查询到期事件；GC 命令也会处理到期事件，另有独立
publish-batch-tick 命令。使用原任务行锁、批次行锁、事件行锁与同事务完成
记录。没有外部 broker 调用，事务失败可以安全重试。重复事件复用已有
operation_inbox 回执；不会重复写入 workflow_events。
只有与批次关联的真实 Rust B 站工作流才记录 publish.batch.finished，
没有关联工作流时仍保留原批次和 inbox 回执，不创建虚构工作流。
事件是状态通知，不重新执行投稿，也不把旧 Hatchet 工作流伪装成 Rust 工作流。

## 延迟清理

从所有当前目标批次最后一次完成时间起至少 24 小时。消费时重新核对
当前批次、任务状态、未完成工作流、投稿、字幕、渲染、配音、P2 和 SAU
任务；过早、仍活动或回执不确定则延迟。旧批次被新批次取代则停止旧清理。
在事务内释放已登记、明确属于该 Rust 任务的 v2/ 中间媒体引用；
保留 video_final、video_dubbed、dubbing_audio、publish_result，
没有 final 时保留最新原始视频。共享对象、其他所有者引用及手动保留期限
不被删除或缩短。已释放对象至少额外保留 1 小时，既有更晚到期时间保持。
明确的 Rust workdir 进入已有 storage_workdirs 清理队列。
实际文件删除由现有 GC 管理，GC 再检查活动任务与未核实投稿。
cleanup_enqueued_at 表示持久清理安排已提交，不表示磁盘已经删除。
成品仍遵循独立 asset_ttl_days 设置（默认 0，保留）；批次流程的 Cleanup
阶段不再因为其他目标尚未完成而误报 B 站失败。

不扫描、不自动接管旧 Python WORK_DIR 或未登记的历史文件；历史遗留
资源继续由单独的安全清理与所有权迁移处理。

## 历史事件迁移

000038 迁移只转换 Rust-owned task 的 pending、attempt_count=0、
无租约、未投递且无原 inbox 操作的两类旧事件。保留原 event ID 与到期时间。
已投递、尝试过、存在租约或旧 Python 所有权的事件不自动重放或抢占。
正式切换前停掉旧生产者及消费者并核实剩余旧事件，旧 Hatchet 工作流
仍需独立迁移/结束。此迁移不是全局 Python Dispatcher 的完整替换。

## 隔离测试

VIDEOROLL_V2_TEST_BILI_DIRECT_ONLY=true bash rust/scripts/smoke-isolated.sh
VIDEOROLL_V2_TEST_PUBLISH_BATCH_EVENTS_ONLY=true bash rust/scripts/smoke-isolated.sh

完整 B 站模拟投稿回归、原生通知幂等、24 小时与最新目标门槛、未知回执、
共享引用、成品/唯一源文件保留、实际 GC unlink、三并发消费、历史事件
选择性接管及重复历史键均经过独立 PostgreSQL 验证。
生产数据库与实际 B 站未修改。
