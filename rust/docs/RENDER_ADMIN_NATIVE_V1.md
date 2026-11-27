# 原 RenderWorker 管理页面与 Rust 原生执行兼容

基线 94b87e1。独立 Rust 部署配置 VIDEOROLL_V2_RENDER_MANAGEMENT_NATIVE=true 后，原 /api/v1/render-management 路径使用原物理 render_workers/render_worker_enrollments 的 rust-v2 域；保留 Python 预览默认契约。原网页邀请生成原生64字符令牌，注册仍用现有 /render-workers/v1/enroll。原生邀请必须 active、未消费、未过期，吊销与注册在同一行锁上竞争；不转换或覆盖历史 Python 节点身份。节点启停、draining、并发、身份设置、凭据吊销限定原生域。节点任务计数据当前有效租约，而非旧 active_jobs 缓存。原生每设备调度未实现，返回409且界面明确禁用，避免只改标签而不影响分配。

执行读取同时保留原 Python 历史与原生 operation_inbox 关联；原生 completed 映射旧 DTO succeeded，未认领的 worker_id 为 null，原生 job_status 来自原 inbox。兼容模式仍只读原 Python 执行及旧字段。原生部署模式下历史执行和原生执行均不直接接受旧管理接口的 cancel/requeue；应使用任务工作流控制，避免绕开租约、上传和回执。历史 P2 重新入队到 Rust 的真实迁移仍未实现。旧 Python 节点须用新的独立 worker_key 重新注册；不暗中替换旧凭据。原生 worker 客户端接受非空令牌；旧 Windows 提示文案仍可能显示 vre_*，未进行真实 Windows/GPU 测试。

验证：cargo check --all-targets 通过；专项实际 API + 独立 PG 通过，测试邀请鉴权、吊销后拒绝、一次性令牌、原生注册和读取、旧域拒写、节点更新、设备控制拒绝、队列/有效与过期租约/完成/历史执行读取、状态/任务/节点筛选、真实有效租约计数、旧重排拒绝与原始状态不变、吊销节点凭据后 heartbeat 401。日志 target/render-admin-isolated.log。未写生产数据、无新表或迁移、未部署或 push。

回归：195/195 Rust 库测试；旧模式同夹具 Python/Rust 差分 46 组通过（历史 P2 重排仍为明确的 409 差异）；前端 TypeScript/Vite 构建通过，环境 Node v20.19.2，正式发布仍需使用仓库要求的 Node 22。日志 target/render-admin-lib.log、render-admin-legacy-diff.log、render-admin-web.log。

## 终态渲染资源回收（2026-11-27）

原 Python 执行记录不使用 uploaded_asset_id 外键，Rust 合并表后该 RESTRICT 外键可能永久保留已结束渲染的产物。保留原表、外键约束和执行记录，仅在现有全量 TTL GC 事务内解除已经核实完成的引用：

- 必须为原生 completed 执行、finished_at 存在、无租约、终态所属工作流、对应 operation_inbox 已 succeeded，且 ticket、task、workflow、mode、asset kind、规范输出路径、执行与 Inbox 回执完全一致。
- 使用父任务锁与非阻塞表锁，不提前解除引用。禁用 TTL、共享/播控引用、活跃/可恢复执行、回执不一致时保留。排队/上传/暂停等状态即使租约过期也不能作为 GC 的安全依据。
- 清理时保留原执行 UUID、output_json、Inbox 结果及日志；metrics.resource_retired=true 标记历史资源已回收。物理删除失败时回滚引用解除和资产行。不会改变外键为 CASCADE，也不会删除执行历史。
- 完成后的迟到回调需要原 Worker 身份、原 fence 和准确资产 ID（若提供）；仅确认已经结算的历史回执，不重建文件或调度新阶段。输出探测仍如实报告没有可用资产。
- 暂不自动解除 failed/canceled/lost、旧 Python 执行、缺少完整回执的原生执行引用；也不放宽手动删除接口。这些保留待后续针对性核验。

专项测试复用 `VIDEOROLL_V2_TEST_RENDER_EVENTS_ONLY=true bash rust/scripts/smoke-isolated.sh`，真实临时 PG/HTTP Worker/FFmpeg 上传夹具验证 TTL、引用解除、失败回滚、回执不一致及迟到回调；不访问生产数据库。
