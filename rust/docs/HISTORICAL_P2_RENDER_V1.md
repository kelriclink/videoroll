# 历史 P2 音频合成任务 Rust 恢复（2026-11-25）

基线 `d4bf368`。旧 YouDub audio-only handoff 已生成混音及对齐 SRT，旧 RenderWorker 失败或丢失时，不需要重新运行 TTS。本轮在原渲染管理 requeue 接口增加安全子集：原 `render_jobs`、`dubbing_jobs` 与 `operation_inbox` 持久化独立恢复记录；原历史 execution 保持不变，不伪造原生 workflow/YouDub 成功证明或外部投稿回执。无新物理表及迁移。

- 仅在原生管理与 native-only outbox 配置启用、旧兼容渲染写入关闭后允许。部署仍要求实际停掉旧 Python/Hatchet/Celery 消费者；配置开关本身不能证明外部进程已停止。
- Rust 所有任务、原 render/dubbing 双向关联、原 `youdub:p2-render:<dubbing-id>` operation key、失败/暂停与失效执行状态、无遗留作业租约、无其他活跃渲染/配音、无不确定工作流/投稿/未结算通知，且无历史最终中配资产或已上传结果。原视频与混音需要同任务资产及完整校验和/大小，对齐字幕用历史内容寻址名称和双向持久化绑定，执行时验证真实文件 SHA-256。
- 原有 `POST /render-management/executions/{id}/requeue` 在事务内更新原 render/dub 状态、插入唯一的历史恢复 inbox；重复同一 execution 请求复用记录。前端显示历史 P2 恢复状态，初筛仅提示候选，实际操作由服务端严格验证。
- Rust worker 中单独的有界合成循环领取队列（每次一个；跨进程 advisory xact gate、原业务记录行锁和 180 秒租约，30 秒续租）。本地 FFmpeg 使用原视频、混音轨和对齐 SRT；以 token 区分临时与最终文件。成功时注册 `video_dubbed`、storage_objects 和 task 引用，并在同一 PostgreSQL 事务内结算原 render/dubbing/inbox，保持原 P1 与 task 状态不变。失败或过期租约持久化失败并释放租约，不自动重跑，不覆盖原旧事件。
- 当前限定本地可读素材和默认字幕风格/缩放、支持的编码器，使用 CPU FFmpeg；不宣称原 ASS 样式像素等价、远程 GPU 或对象存储读取兼容。每个历史恢复 operation 只允许一次执行；失败后的重新授权/再入队策略待补齐。真实大文件、续租中途断线、并发资源回收、崩溃于文件 rename 与事务 commit 之间的孤儿文件、全量旧生产配置尚未验证。已完成媒体仍须下一阶段独立适配 P2 追加投稿的来源证明及平台回执核验；本轮绝不自动修改任何稿件。

验证：独立 target `/4t/videoroll-rust-historical-p2-render-target`，隔离 PostgreSQL 35474。`VIDEOROLL_V2_TEST_HISTORICAL_P2_RENDER_ONLY=true` 专项通过，真实 FFmpeg 视频/音频与 HTTP 三路并发重排、三路并发 tick、唯一带音轨中配结果、原 ID 和原 P1 不变、重复请求幂等、损坏字幕、旧所有权、错误绑定、活跃旧租约和 Hatchet、过期本地恢复租约均覆盖。`VIDEOROLL_V2_TEST_RENDER_ADMIN_ONLY=true` 和 `VIDEOROLL_V2_TEST_RENDER_EVENTS_ONLY=true` 两组回归均通过，前端 TypeScript/Vite 构建通过（Node 20.19.2，正式 Node 22 CI 未执行），Rustfmt/Python 语法/Bash 语法/Git 差异检查通过。未执行全量 217 条业务等价验收、真实 GPU、生产数据或真实投稿。
