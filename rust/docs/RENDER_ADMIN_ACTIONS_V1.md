# 原生远程渲染管理取消与重排（2026-11-25）

基线 `4e1c648`。原生模式下沿用旧渲染管理 HTTP 路径，开放安全子集，不改变历史 Python 模式行为。

- 只处理 Rust 所有任务、精确关联的原生 render 执行/工作流/awaiting inbox/ticket，父工作流必须 running 且当前阶段 render；其他活跃或不确定阶段拒绝。
- queued 或 running/uploading 且租约已过期、未注册上传输出，允许取消；事务内按 task -> workflow -> job -> execution 加锁并结算四者为 canceled，保留原执行记录，重复请求不增加事件。
- 同范围允许重排，但必须未达到五次领取上限。重排保留执行 ID、inbox、ticket 和 attempt，清除 worker/fence/lease，恢复 queued；已 queued 的重复请求幂等。不直接重启取消后的工作流。
- 当前有效租约、已注册上传结果、已完成执行及历史 Python 执行仍禁止原生管理直接操作。前端分别读取 cancel_supported/requeue_supported，保留旧 control_supported 兼容字段。

验证：使用独立 target `/4t/videoroll-rust-render-admin-actions-target` 和临时 PG 端口 35471，`VIDEOROLL_V2_TEST_RENDER_ADMIN_ONLY=true bash rust/scripts/smoke-isolated.sh` 成功。真实 API 测试覆盖三路并发取消/重排、一次事件、预算不重置、状态一致、有效租约/上传结果/错误票据/完成/历史记录保护。Rustfmt、git diff 检查、Python 语法及前端 TypeScript/Vite 构建通过；本机 Node v20.19.2，正式 Node 22 CI 未执行。未运行全量 217 接口差分、实际远程 GPU 或生产数据验证。活跃执行的协作取消、含上传结果的安全重排、历史 P2 重新入队仍待开发。
