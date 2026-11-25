# 历史 P2 追加投稿原生恢复（2026-11-25）

基线 `d6debff`。复用原 `append_p2`、`p2`、`p2/resolve` HTTP 接口与 Bilibili P2 Worker；历史任务不制造原生成功工作流，不修改原 P1 publish_jobs 记录。迁移 20261125000041 扩展原 operation_inbox 的 P2 检查约束、兼容视图及候选索引，无新物理表。历史追加记录使用原 publish_job UUID，P2 FK 指向历史渲染恢复 inbox，HTTP 回执还原历史 dubbing UUID。若 ID 冲突则拒绝，不覆盖其他 operation。

- 仅原生管理/native-only outbox、旧渲染控制关闭且 Rust 所有的已发布任务。原 P1 需要 web 回执、正数 AID、BVID、唯一分 P 和原 UPOS biz_id；拒绝多 P、重用回执、显式非默认账户、租约、其他未结算发布/工作流、同稿件其他 intent。历史 auto_dub_p2 只接受不存在或没有上传/CID 证据的 failed，拒绝其余历史状态。原生与历史 enqueue 共用按 AID 的事务级互斥检查。
- 历史中配须为上一轮 Rust 恢复完成的同任务 dubbing/render/inbox/asset/storage ledger 精确关联；确认 SHA-256、大小、live object 与 task 引用，Worker 网络调用前复验数据库与真实字节。入队事务固定媒体引用。作者侧在上传前和编辑前核对 AID、首 P CID、可用 BVID 与分 P 数，绝不追加第三 P；编辑后模糊结果保持 unknown，只允许核对作者侧已登记上传 CID 后标记 published，不允许直接认证 failed 后重发历史稿件。
- 共享 Worker 上传回执写入 submitting 而不是暴露为可被第二 Worker 领取的 uploaded，后续作者核验失败按 unknown 保守处置。已有 uploaded 回执仍支持领取。历史任务详情列出恢复成功的旧配音记录、原发布记录及追加状态，失败历史 intent 不显示再次追加入口。
- 限制：默认 B 站账号且有完整旧单 P 回执、上一轮恢复的本地中配资产、显式操作；未迁移任意旧完成配音的直接接管、历史排队/上传/unknown 的批量接管、非默认账号、失败后重新授权、历史原生字幕同步、自动追加。配置开关不证明 Python/Celery/Hatchet 已停止，实际切换须独立验证。旧/新共享发布链的独立全量回归、长上传/故障注入、真实 UPOS/作者中心、原生产数据迁移尚未验收。

验证：独立 target `/4t/videoroll-rust-historical-p2-append-target`，隔离 PostgreSQL 35475。`VIDEOROLL_V2_TEST_HISTORICAL_P2_APPEND_ONLY=true` 第二轮完整通过（第一轮非 Rust 任务原入口 404 而夹具预期 409，修正预期后重跑）。真实 API、PG 和 FFmpeg MP4、模拟历史恢复回执与模拟 loopback 作者中心；仅注入预上传回执，mock HTTP 下真实 UPOS 被禁止。三并发 enqueue/tick 仅一次 edit、P1 元数据和原发布行不变、原历史 ID、原任务详情读取、503 模糊结果后不重放、作者 CID 正向核验、首 P CID 错误/媒体损坏/历史 unknown 或 published/错误资产/旧所有权/多 P 拒绝均通过。`VIDEOROLL_V2_TEST_HISTORICAL_P2_RENDER_ONLY=true`、`VIDEOROLL_V2_TEST_RENDER_EVENTS_ONLY=true` 回归通过。Rust 编译/格式、前端 TypeScript/Vite（Node 20.19.2）、Python/Bash 语法、Git diff 检查通过。未执行现有完整原生 P2 专项、全 217 条业务差分、Node 22 CI 或真实生产操作。
