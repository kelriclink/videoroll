# 历史任务所有权接管及恢复

不复制业务表，不改变原 tasks.id、assets.id、storage_key、publish_jobs、
平台稿件 ID 或历史工作流 ID。原 tasks.runtime_owner 是管理权来源。
接管回执保存在原 operation_inbox，runtime_engine=rust-task-cutover，
operation_key=task-ownership-cutover:<task UUID>。重复接管返回原回执。

## 审计与执行边界

task-ownership-audit --limit 100
task-ownership-audit --after <UUID> --limit 100

只读分页审计 Python 所有任务，给出数据库阻塞原因与可能的恢复阶段。
分页不是全库就绪结论；跨页之间可能发生变化，正式切换必须停机。
媒体尚未在审计阶段校验。

正式接管前备份数据库，并停止所有旧、新业务生产者、消费者、调度器、
远程工作进程及可能写入历史媒体的程序。确认 Hatchet/Celery/外部平台
没有未完成操作。使用 VIDEOROLL_V2_NATIVE_OUTBOX_ONLY=true。
task-ownership-adopt --task <UUID> --quiesced
只转移管理权，保持原任务状态，不创建工作流。
task-ownership-adopt --task <UUID> --quiesced --resume
验证原媒体后接管并恢复安全的阶段。没有全量强制接管命令。
--quiesced 是明确的操作者确认，不是自动探测。

--stopped-run <UUID> 可重复传入，必须覆盖该任务全部未终态 Hatchet
运行 ID；仅在外部已独立核实这些运行停止后使用。原记录标记
superseded，保留 external_run_id，不声称成功。launch_failed 也视为
可能的未知外部提交，不自动忽略。其他未终态原作业、租约、投稿、
Inbox/Outbox 阻塞仍需先独立结算。任何拒绝都回滚整个事务。
NOWAIT 数据库锁防止抢占活跃写入，但不能证明外部服务已经关闭。

## 恢复策略

- created/ingested YouTube：从下载恢复，必须有原来源 URL。
  仅未尝试、无租约/结果的原 download Inbox 可在同一事务退休，
  并创建新的 Rust 下载阶段；不允许接管-only 丢弃等待下载的任务。
- downloaded/audio_extracted/asr_done/translated：从字幕恢复，
  现有可信 SRT 可供原生阶段复用。
- subtitle_ready：从渲染恢复，需要原视频和 SRT。
- 已渲染、待审核、已批准、失败、已发布、已取消：保持原状态，
  不自动推断后续阶段。
- 未知来源授权、已有投稿历史、缺失/损坏文件、完成下载回执与
  实际资产不一致、原视频/字幕/最终视频与恢复阶段矛盾，禁止自动恢复。

原媒体路径检查禁止越界和符号链接，验证非空、SHA256/size（若原
记录存在）、视频 ffprobe、SRT 基本时间线标记。保留原路径，不做
完整语义一致性认证，不扫描或搬移旧 WORK_DIR。不自动建立历史
资产的 GC 所有权。恢复后的原生工作流固定关闭自动投稿及自动配音，
直到人工明确发起后续操作。原生调度器只领取 Rust-owned task。
手动重启允许显式接管后的原资产路径；已有投稿历史禁止通用流程重启，
需独立核实并使用对应投稿/P2操作。
未接管或无法核实的历史任务继续保留。不能以删除记录或伪造终态
绕过迁移阻塞。

## 验证

VIDEOROLL_V2_TEST_TASK_OWNERSHIP_ONLY=true bash rust/scripts/smoke-isolated.sh

独立 PostgreSQL + 真实 FFmpeg 小视频验证原 ID/媒体保留、分页、
原子接管、并发幂等、旧下载退休、损坏/缺失媒体拒绝、未知投稿、
外部停止确认、真实 Rust Worker 渲染及强制关闭自动配音/投稿。
未运行生产库接管；旧任务的未决外部执行仍须切换时核实。
