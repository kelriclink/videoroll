# VideoRoll Rust v2 功能差异

当前不满足完整生产替换门槛。

| 系统 | 隔离验收已覆盖 | 仍需完成 |
|---|---|---|
| 任务/调度 | Postgres 事务、租约、去重、Unknown 待核实；旧版 TaskRead、新建本地/YouTube、停止恢复、隔离工作流（仅 V2 自有任务） | 所有旧 Hatchet 任务迁移、完整 217 API、跨机器重启与并发压测 |
| 影音下载 | YouTube 来源扫描、单视频、现有文件续用 | 真实 yt-dlp/aria2 代理、并发和断点续传 |
| 字幕 | Whisper 兼容 API、真实 FFmpeg SRT/ASS 硬字幕 | 全部 ASR Provider、长视频与原版特殊参数 |
| 翻译 | Index-Translate 模拟请求、缺行补译、Provider 选择 | 正式 API 配额、术语与长上下文质量 |
| RAG Agent | 有限工具调用、知识检索和证据入库 | 多代理、字典、全部网络研究、原版语义召回 |
| YouDub P2 | 独立模拟 bridge、确定性 ID、P2 视频绑定 | 真正 VoxCPM2/Demucs GPU 和配音对齐 |
| 渲染 | 本地真实 FFmpeg、远程 Worker 协议模拟 | Windows/Linux 真 GPU、长时间稳定性 |
| B 站投稿与字幕 | 原生 UPOS、Unknown 投稿保护；原生 SRT 独立同步、双 P CID 验收、疑似已提交时阻止自动重试、审核后恢复 | 真账号双 P 投稿、活动、字幕草稿与合集真实验收 |
| 其他平台 | 仅兼容扩展点 | 抖音、小红书、快手原版 SAU 功能 |
| 播控 | 中文文件名导入、引用保护、解除后回收 | ffplayout 真正节目单编辑与播放控制 |
| 存储/媒体 | 引用式 GC、播控资产保护、V1 合同形状的成品/资产列表、真实 Range 流媒体、FFprobe 校验并事务提交的本地 multipart 上传 | 大规模压力、复杂原版上传选项、旧资源迁移 |
| 前端 | Rust V2 独立页、原版 React 全页面隔离预览 URL 开关、原生 V1 读写适配；开发台可看到 API 差异清单 | 193 个旧 API 前端引用逐个移植、完整页面交互/状态/E2E |

原版 orchestrator 含至少 131 个带路由装饰器的端点；Rust 当前约 53 处路由声明。此数量不能直接换算功能覆盖率，但提示需要逐接口对照。旧 API 只读兼容层不是 Rust 原生实现。

隔离脚本使用模拟远程服务和合成视频，不能替代真实发布和 GPU 验收。只有通过全部原版契约、故障注入、真实设备运行和可回滚部署后，才允许评估生产切换。


## B 站原生字幕独立同步

- 使用 Rust src/bili_subtitles.rs、SQLx 与 reqwest，无额外投稿 SDK。
- 仅扫描已确认、带 native_bilibili=true 的 Rust 投稿回执，不会误写旧版或其他平台。
- 独立持久化 aid、bvid、SRT SHA-256、分 P cid、提交状态；字幕失败不改变 PUBLISHED。
- 受保护的管理 API 可列出任务、查看详情、在真实核实后手工处理 unknown。
- 后台 Worker 每 45 秒扫描回执并处理；重启可以继续安全阶段，preSave 未知结果拒绝重复提交。
- 最多 8 次安全阶段重试，源字幕引用上限七天，防止磁盘长期堆积。
- 隔离测试覆盖单 P、双 P、HTTP 503、重复任务执行及人工核实后的恢复。
- B 站投稿/字幕接口属于非公开稳定 API；真实账号与 B 站服务器仍需验收。


## 2026-11-22：旧版 API 与前端迁移审计

- 源码定义 217 个 FastAPI 路由装饰器；原版 orchestrator 有 132 个。原版前端有 193 处旧 API 文本调用候选（160 种 URL 模式），动态路径可能遗漏。
- rust/scripts/audit_legacy_routes.py 生成清单，--check 检查变动。rust/docs/LEGACY_ROUTE_CATALOG.json 和 FRONTEND_API_REFERENCES.json 用于逐项复核。Rust /api/v2/parity 返回源声明数量与原生适配数量，全功能完成标志仍为 false。
- Rust /api/v1 局部兼容层仅处理 V2 自有任务。原生适配包括管理员验证、任务列表/详情、YouTube 手动/自动入库、本地视频上传、停止/继续、字幕/配音/投稿任务历史、成品/资产查询、HTTP Range 视频读取、播放链接、Linux 系统资源。
- V2 自有手动任务由 vr2_managed_tasks 表标记，不会将 V1 任务作为 V2 所有任务修改/调度。
- VITE_RUST_V2_FULL_UI=true 可构建原版 React 页面的 Rust 隔离预览；通过 VITE_RUST_V2_LEGACY_API_URL 指向独立 Rust。默认不开启。前端页面存在或构建通过不表示所有交互可用。
- 未实现的功能返回明确 404/错误，不会偷偷转发给旧 Python 写入 API。
- 隔离 Postgres+FFmpeg 验收包括任务建立、multipart 流式上传、源类型/资产归属、重试恢复、Range 读取及 YouTube 入库。
- **生产门槛未达到：真实多平台投稿、原版高级工作流和大量独立服务 API、完整前端交互仍未通过契约验收。**

## 2026-11-22：投稿元数据与 YouTube 元数据原生迁移（第二批）

- 原版 React 投稿编辑器的 GET/PUT /api/v1/tasks/{task_id}/publish_meta、GET/POST .../publish_meta/draft 已改由 Rust 原生实现，没有 Python 兼容代理。
- Postgres vr2_publish_meta 保存投稿信息、SHA-256 与修订版本；仅 V2 所有任务可编辑，投稿作业产生后及结果未知时禁止改稿。
- 原生 B 站提交读取已保存的标题、分区、标签、转载来源、话题、活动及支持的高级 Web 字段。额外字段白名单以外明确拒绝，不允许悄悄忽略。
- topic_id、mission_id 只做正整数结构校验，尚未接入 B 站活动报名资格核实，不能认定为已认证活动。
- 草稿模式 default/source/auto 已接入。源元数据来自真实 yt-dlp 元数据缓存。不自动杜撰分区，也不假装完成 AI 标题翻译或 AI 分类。
- 原生 GET /api/v1/tasks/{task_id}/youtube_meta、POST .../actions/youtube_meta 使用维护中的 yt-dlp 单视频 JSON 提取能力（独立进程、输出上限、ID 校验及超时）。原始 JSON、校验和及任务标题记录进入 V2 独立资产存储。
- GET .../youtube_download_progress 读取真实 V2 下载作业状态和已登记文件大小。正在下载阶段尚未提供细粒度字节数、速度和 ETA；不能把这种兼容接口等同于实时下载进度已全面迁移。
- 原版 YouTube proxy、cookies、aria2c 并发配置和实时进度、完整 AI 审稿、其他高级页面功能仍未迁移。审计新增 7 个原生兼容入口（总计 31），不代表 31 个完整功能均达到等价。

## 2026-11-22：YouTube 原生下载控制 / AI 审核 / 活动 ID 防误报（第三批）

- 原版 React YouTube 页面可直接访问 Rust 原生的 GET/PUT /api/v1/settings/youtube 及 POST /api/v1/settings/youtube/test。支持 HTTP(S)/SOCKS5 代理（不接受嵌入账号密码）、Cookies.txt Netscape 导入、1–16 连接并发和兼容客户端 player_client。V2 Settings 独立 overlay 不改旧版生产配置；不自动解密原版 Fernet Cookies。Cookies 在私有目录以 0600 权限保存，提取器持有独立临时副本。
- 下载阶段真正读取上述配置，使用 yt-dlp 子进程续传；内置原生 HLS/DASH 并发。只有确认 yt-dlp 至少为 2026.06.09 且 aria2c 可用时才启用 aria2c 的普通 HTTP(S) 下载器；SOCKS5 代理下不会强行启用 aria2c。没有安全版本校验则退回 yt-dlp 原生分片路径。HTTP 与 HLS/DASH 不得混用下载器。
- 通过 stdout 受限前缀和长度解析实时已下载字节数、估计大小、速度、ETA、百分比。vr2_download_progress 只存数字和作业 ID，并用当前任务租约、有效期和 workflow state 限定写入，阻止旧执行覆盖新任务的进度。原版 GET /api/v1/tasks/{id}/youtube_download_progress 会丢弃已失效执行的速度/ETA，不再虚报。
- 原生 GET/PUT /api/v1/settings/review 和 GET/POST /api/v1/tasks/{id}/publish_review /actions/publish_review 提供基于真实 V2 SRT 文本的违禁词/AI 审核。AI 模型使用现有翻译模型 Provider/ID 与 OpenAI 兼容 JSON 接口；无模型、模型报错、模型未返回明确布尔结论都会判定为不通过，绝不猜测为通过。审核指纹绑定当前稿件、审核规则和 SRT，发生变化后旧结论失效。真正 UPOS 投稿前调用审核门槛；禁用审核仅限显式设置。
- B 站原生上传在存在 topic_id / mission_id 时，从已登录创作中心固定 HTTPS 只读查询精确核对话题/投稿任务 ID。公开活动列表 ID 不会被当作真正的 mission_id；活动规则要求有实质内容。转载视频只有规则明确允许转载且没有报名、白名单、原创等未证实限制时才通过；查询失败、不唯一或不匹配时阻断上传。可选 metadata.activity_name 作为检索关键字，此本地辅助字段不会转发给 B 站投稿接口。该检查不能证明账号奖励或报名资格，不能说成已完成活动资格认证。
- React Rust 全页面预览下 YouTube 页面隐藏尚未迁移的「首页推荐定时扫描」，并明确说明 HTTP(S) aria2c 安全限制和未迁移的大文件下载阈值，而不是展示可点但无效的设置。
- 未覆盖部分：YouTube 首页推荐扫描、Cookies 加密迁移、下载限速/128 MiB 阈值、B 站活动账号级报名状态、AI 自动活动筛选、多平台 SAU 投稿、手动字幕与 YouDub 全量控制、其余旧版 API 和页面。老生产服务继续运行，新 Rust 只隔离验证。API 候选迁移计数约 38/217，且每个入口的业务等价还需逐项测试。

## 2026-11-22：Python 对照迁移第四批：任务详情页手动工作流及审计矩阵

此批为部分兼容，不是 217 个 Python API 的完整迁移。单个 V1 原生入口仅在功能、状态与安全约束等价时才能标为完成。

- rust/src/scheduler.rs 修正 V2 workflow start 的任务所有权：必须关联 vr2_managed_tasks。独立工作流分代可从 download/subtitle/render/dubbing/publish 精确阶段启动，沿用之前设置的浅层结构及字幕、渲染配置内层合并，不会为了重做字幕重新下载原片。
- 重启存在强制外部副作用栅栏：此前 pending/running/awaiting/unknown 或曾真实成功的 publish 阻止重启；明确标记 skipped 且没有外部副作用的 publish 不被误当作真实成功。活动中同阶段的重复操作只有设置一致才复用原作业；变更设置返回冲突。
- 新增旧任务详情页 V1 原生 POST actions/subtitle、actions/subtitle_resume、actions/publish 和 tasks/actions/resume_failed_recent。仅支持 Rust 已实现的 ASR/翻译和 FFmpeg 字段；无法原生执行的来源选择、引擎、特定字幕、自动发布策略和发布附加选项明确报错，绝不接受后偷偷忽略。手动投稿只支持保存过审核元数据、启用 native Bilibili 的安全部分；禁止强制重试和跳过 AI 审核。
- 新增当前 Python YouDub VoxCPM2 路由 actions/dubbing、dubbing_jobs/{job_id}/retry、dubbing_jobs/{job_id}/control。只接受成功的同任务最新字幕作业与最新原片；operation_key 不可复用在不同请求数据，原始已完成请求可幂等取回。重新尝试只能创建新的持久代际。请注意，此控制目前仅管理 Rust workflow 状态与租约，不保证正在 GPU 上执行的第三方推理进程立即停止；原版已退役的逐段改写、预览和确认不予伪装复活。
- 原生 V1 GET /settings/publish/platforms、PUT /settings/publish/platforms/{platform} 已基于单独 V2 overlay 提供 B 站开关。由于抖音、小红书、快手 SAU 账号和投稿尚未完成，主动启用它们会返回 409；管理页不能误显示可用。这不算 SAU 功能迁移完成。
- 修复跨代际 P2 关联逻辑：手动字幕、手动 YouDub、手动 B 站投稿可以分别属于不同工作流。只能在成功的 vr2_dubbing_runs 证明原视频、SRT 与 P2 输出都匹配、同任务归属并且 YouDub 作业成功时使用，不能因为 workflow_id 不同而误拒绝，也不能拿旧配音视频冒充当前配音。
- 增加 rust/scripts/audit_contract_matrix.py 与 rust/docs/CONTRACT_MATRIX.json、CONTRACT_MATRIX.md：对 217 个原始 Python 路由逐项记录来源方法、V1 原生映射与全面验收状态。目前所有路由的 fully_verified 均为 false。审计统计仅表示候选入口数量，而非实现率。
- 隔离验收新增真实 PostgreSQL 阶段代际与任务归属、同阶段重入、失败批量恢复、未知稿件阻断、平台开关 V2 隔离、真实执行的模拟 YouDub 手动创建及成功资产关联。原生 YouDub 与最终 B 站 P2 仍需真实设备和平台验收。

目前仍未完成：SAU 多平台账号及登录/投稿、B 站原版全部设置与追加 P2、ASR 引擎全覆盖、字幕高级控制、完整 YouDub 控制到第三方 GPU 运行时、渲染管理页面、告警与实时 WebSocket、旧版全部前端操作，以及故障注入、实际 YouTube/真实 B 站生产级验收。生产 main 与 VideoRoll 服务不变。
