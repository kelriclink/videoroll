# 原生远程渲染上传后回执恢复（2026-11-25）

基线 `abd7671`。处理上传接口已完成 ffprobe、文件写入和 fsync、资产与存储引用事务，但 Worker 未发送 complete 或网络中断的情形。

- 已注册上传资产的执行不得再次被 Worker 领取，也不进入五次租约重试耗尽失败分支；失败回调不得覆盖已提交的上传证据。
- 原生 render-tick 对 queued 或租约过期的 running/uploading 且具有上传资产的执行进行恢复；验证精确 task/workflow/job/ticket、运行态与 Rust 所有权、资产 ID、预期路径、非零文件大小、SHA-256、live storage_objects 和 workflow/video_final 引用；哈希在行锁外计算，提交时重验关联和租约。
- 恢复先持久化原执行的 completed 回执，再复用已有调度器结算流程；保留原 attempt 和 Worker fence，重复回调仍可按原回执幂等返回。不生成新执行，不引入 Python 调度或新表。
- 每轮最多验证 4 个上传候选；无效文件、存储证据缺失暂缓并记录警告，不自动投稿、不重渲染、不删除文件。有效租约不接管。完整哈希可能带来磁盘读取成本；错误候选仍需告警/人工处置与退避优化。未覆盖活跃 Worker 协作取消、未提交资产登记的文件孤儿、历史 Python P2 重新入队或多机长期压力测试。

验证：独立 target `/4t/videoroll-rust-render-upload-recovery-target`、临时 PostgreSQL 端口 35472，`VIDEOROLL_V2_TEST_RENDER_EVENTS_ONLY=true bash rust/scripts/smoke-isolated.sh` 两次成功。真实 FFmpeg 生成 MP4，经真实 multipart HTTP 上传，模拟第五次领取后回执丢失、三路并发恢复、精确持久回执和唯一下一阶段，重复 complete 幂等。损坏文件（同长度、不同 SHA-256）、有效租约保护、尚有预算的已上传执行不被领取、上传后 fail 拒绝、既有重试上限及历史 P2 取消回归通过。未修改生产数据、未触发真实投稿，未执行全 217 接口差分。
