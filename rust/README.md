# VideoRoll 2.0 Rust 重写项目

当前状态：独立实验分支 rewrite/rust-v2。不能直接替换生产版。
原版主分支和部署不变，原版任务/配置/告警表正在作为唯一事实源；剩余 `vr2_*` 状态表仍待逐项合并，不能直接替换生产服务。

2026-11-23 静态合同审计：**147/217** 条 V1 API 已部分原生适配，尚无完整生产等价签收。词典批量导入直接写原版 PostgreSQL；知识库列表、删除、术语/文档写入、在线 OpenAI 兼容 Embedding 及向量重建已接入。Local SentenceTransformers/OpenVINO、知识批量导入、生产 Agent/Embedding 全链路尚未完成。

## 功能范围

| 子系统 | 目前代码支持 | 尚缺生产等价/真实验证 |
|---|---|---|
| 核心 | Rust Axum、Tokio、SQLx、Postgres 原子任务创建、去重、租约与 fence | 全部老 API、历史 Hatchet 任务迁移 |
| 认证 | V1 兼容管理员 Cookie、密码、V2 token、限速 | 全部原版会话/审计和代理场景 |
| YouTube | yt-dlp 调度、频道/列表 CRUD、来源去重及定时扫描 | aria2 高并发续传、复杂代理与真实来源重试 |
| 字幕 | Whisper 兼容音频请求、分窗识别、指纹检查点、SRT/ASS、按编号翻译 | 所有原版 ASR、Index-Translate 和 RAG Agent 完整实现 |
| 字典 / RAG | 词典 CSV/TSV/ECDICT/CC-CEDICT/JSONL/TMX/TBX 流式导入、CRUD、词条提升；原版知识项列表/新增/去重/删除、在线 OpenAI 兼容 Embedding 向量入库与批量重建 | local/OpenVINO 向量生成、知识库批量文件导入、完整 Agent 生产消费链路 |
| Embedding | 原版设置驱动的模型列出/下载/删除；pgvector/HNSW 诊断；在线 OpenAI 兼容 `/embeddings` 实际调用、维度校验与持久化 | 真正本地 SentenceTransformers/OpenVINO 推理、Fernet 历史密钥恢复、全部 Agent 查询链路 |
| 视频 | 真正 FFmpeg H264/HEVC/AV1、VAAPI 回退、ASS 烧录 | 复杂原版 RenderSpec、长视频、多 GPU 性能 |
| 投稿批次与封面 | 只读原版 publish_batches 真实回执及最新作业结果；V2 自有任务的 JPEG/PNG/WebP 封面限额上传、FFprobe 验证与 FFmpeg 规范化、原子落盘与资产引用；废弃 YouDub confirm/rerun 返回 HTTP 410 | 大体量封面压测、真实平台多账号批次完成与资源清理 |
| 远程渲染 | V1 Windows Worker 协议，单次注册、独立凭证、领取、心跳、日志、Range、fenced 上传与完成 | 真实 Windows/Linux GPU 节点压测和断电恢复 |
| 配音 | YouDub 独立推理进程桥接，稳定任务 ID 和 V2 P2 归属约束 | 真正 VoxCPM2/Demucs 对齐和音质/时长验收 |
| 投稿 | Rust B 站 UPOS 分块上传、单次稿件创建、双 P 校验；可选原版投稿兼容层 | 真实账户测试、原生字幕/合集/话题/多平台对齐 |
| 资源 | 引用登记、延迟清理、故障/取消回收、播控保护、回收锁 | 海量并发、异常重启和 V1 历史目录清理 |
| 配置与 UI | 版本化设置、队列遥测、V2 独立 React 开发控制台 | 全部原版页面和 API 完全迁移 |

## 开发环境与安全

在 rust 目录运行 cargo fmt --check、cargo check --all-targets --locked、cargo test --all --locked、cargo clippy --all-targets --locked -- -D warnings。
在项目根目录运行 bash rust/scripts/smoke-isolated.sh，测试会创建一次性的回环 PostgreSQL Docker 容器，模拟 ASR 和投稿后端，通过 FFmpeg 真正编码合成视频，并联调完整 Remote Worker HTTP 协议。测试不会执行真正平台投稿。
V2 API 只监听指定回环端口，必须指定独立 DATABASE_URL、VIDEOROLL_V2_STORAGE_ROOT 和强内部 Token。不会在 API 启动时自动迁移或启动 Worker。
YouDub 模型必须使用独立 V2 运行目录，不能和原版共享。原生 B 站投稿和旧版投稿兼容适配器都需要专门的显式配置才会启用。
V2 React 页面只有在 VITE_RUST_V2_UI=true 时出现，生产界面默认没有变化。
原版 Windows Render Worker 通过单次使用的注册码加入，凭证与执行 token 都只属于 V2，不能访问其他作业的数据。

## 关键模块

- rust/src/api.rs：业务 API
- rust/src/scheduler.rs：工作流、租约、恢复、UNKNOWN 人工对账
- rust/src/storage.rs / assets.rs：对象、HTTP 流、延期回收
- rust/src/media：字幕识别、FFmpeg、本地与外部推理
- rust/src/remote_render：现有 Windows Render Worker v1 协议
- rust/src/bilibili.rs / publish.rs / publish_legacy.rs：平台投稿
- rust/src/sources.rs：频道/列表扫描
- rust/src/settings.rs / telemetry.rs：动态配置和队列统计
- rust/migrations：V2 表与权限约束
- rust/scripts/smoke-isolated.sh：隔离集成验收

## 完整替代前必须通过

1. 原版业务 API、设置、217 个路由和全部 React 页面及历史任务双向契约。
2. 所有 ASR Provider、Index-Translate 专用接口、RAG Agent/Embedding/知识库、YouDub 和字幕详细样式。
3. Windows Render Worker 实体 GPU、ffplayout 播控导入、长视频和中断恢复。
4. B 站多账号双 P、话题活动、合集、原生字幕、其他 SAU 投稿平台真实验收。
5. 多 Worker 并发、重启断点、无丢任务、无重复投稿、终态资产可靠回收。
6. 和旧版只读影子流量比对及完整回滚预案，合格后才能考虑切换。

本分支仍不满足全部生产等价条件。没有假称完成所有业务，也没有切换生产。

## Rust 原生 B 站字幕同步

- Rust Native Subtitles 使用持久化任务同步已成功投稿的 Simplified Chinese SRT（B 站 language=zh）。
- 每个 cid 保存独立完成记录；如果 preSave 请求结果未知，则必须人工核实，不能重复提交。
- 不影响已成功投稿的稿件。上限 8 次准备阶段重试，最多七天的字幕源文件引用。
- 开启原生投稿必须显式设置 VIDEOROLL_V2_ENABLE_NATIVE_BILIBILI=true；原生字幕默认开启，可由 VIDEOROLL_V2_BILIBILI_SUBTITLES=false 关闭。
- 仅本机隔离测试允许配置 VIDEOROLL_V2_BILIBILI_API_BASE=http://127.0.0.1:18084，真实账号需独立验收。

## 新增的 RAG / Index / 播控与迁移桥

- rust/src/media/index_translate.rs：独立官方 Index-Translate 请求与字幕 idx 缺行修复，无需泛 OpenAI 路由伪装。
- rust/src/providers.rs：按原版模型 Provider/模型选择配置解析，不存在的选择明确报错，不含密钥的公共 Provider DTO。
- rust/src/rag.rs：global/task 范围的知识库、中文/英文词汇检索、Embedding 余弦混合排序、受限上下文和持久 Agent 研究记录。
- rust/src/playout.rs：独立媒体链接表、校验后的中文平铺视频名、硬链接/复制与保守撤销，不承诺已经插入播控节目单。
- rust/src/compat.rs：默认关闭、显式只读的旧版 API 代理，用于旧页面迁移期间做功能对照，不代表代理接口已经 Rust 原生重写。

以上模块仍需和生产版 RAG、ffplayout API、模型配置进行完整语义对照。参见 docs/INDEX_RAG_PARITY.md、docs/FEATURE_PARITY.md 和 docs/PLAYOUT_MIGRATION.md。


## 旧版 API 与全页面 Rust 试验迁移

- python3 rust/scripts/audit_legacy_routes.py 生成旧版路由与前端 API 调用审计；--check 检查与源码是否同步。明细保存在 rust/docs/PARITY_AUDIT.md 与 JSON 文件。
- Rust /api/v1 为局部原生契约兼容层，限制 V2 任务所有权；/api/v2/parity 显示真正差异，不将同名路由当作完全兼容。
- 可选试验构建 VITE_RUST_V2_FULL_UI=true；ORCHESTRATOR_URL 切换为 VITE_RUST_V2_LEGACY_API_URL（默认 /rust-v2/api/v1），默认生产未启用。
- 本地任务由 POST /api/v1/tasks 和 POST /api/v1/tasks/{id}/upload/video 创建并流式传输；YouTube 手动与自动入口分别为 /api/v1/youtube/ingest 和 /api/v1/auto/youtube。
- POST /api/v1/tasks/{id}/actions/stop 与 /resume 使用 Postgres 状态事务保留原任务状态，外部结果不确定的投稿不能自动重发。
- 还有大量原版 API 和对应页面操作未迁移，**完整转 Rust 仍未完成**。生产原版必须保留。

### 原版投稿草稿及 YouTube 元数据（隔离版）

- 原版投稿页 GET/POST /api/v1/tasks/{id}/publish_meta/draft 与 GET/PUT /api/v1/tasks/{id}/publish_meta 会将编辑内容持久化到独立 vr2_publish_meta，实际原生投稿优先读取此处。
- 原版任务页 GET /api/v1/tasks/{id}/youtube_meta 与 POST /api/v1/tasks/{id}/actions/youtube_meta 使用外部 yt-dlp 提取；需在运行环境提供该程序。原生 GET .../youtube_download_progress 提供可信作业状态，但未提供下载期间的细粒度带宽信息。
- 这些入口不代表全部原版 API、AI 审核、活动核验及前端完整功能已完成；生产 Rust 替换仍不允许。

### 第三批旧版前端 API 迁移（隔离）

- Rust /api/v1/settings/youtube 现有代理、Cookies（受限私有文件）、并发、兼容模式设置及代理连通性测试。下载 worker 直接读取这些设置，利用 yt-dlp 与（经版本校验的）aria2c；任务详情可读取带租约 fencing 的实时下载状态。
- Rust /api/v1/settings/review 与 /api/v1/tasks/{id}/publish_review 及 POST /actions/publish_review 支持规则审核、实际 OpenAI 兼容模型审核、内容指纹与变更失效，拒绝或无法核实时阻止原生 B 站 UPOS 上传。
- 存在 B 站话题/任务 ID 时，原生发布在上传前用登录的创作中心只读接口核验一一对应、基础规则及转载限制；不能证明账户已经报名或具备活动奖励资格。
- 仅 rewrite/rust-v2 开发分支可开启旧 UI 的 VITE_RUST_V2_FULL_UI=true 预览；尚未完成自动扫描、多平台 SAU、全部字幕配音管理 API，不能直接切到生产。

### Python-to-Rust 第四批：旧任务详情页手动操作（隔离开发）

- 新增按 V2 所有权与 SQL 事务约束的 restart_at(stage)；重复字幕点击不重下视频，成功或不确定的外部投稿不会自动重投。
- Rust /api/v1 已接入手动字幕、字幕继续、B 站手动投稿、可逆阶段失败批量恢复、YouDub 原生手动作业与受限重试/控制。只接受现在真正实现的旧字段；超出范围时显式冲突/错误。
- Bilibili 多 P 校验允许不同 V2 工作流代际中的相同原片/SRT 成对成功 YouDub；旧稿件和未成功配音不得复用。
- 原生投稿开关支持独立 Bilibili 与显式启用的 SAU 社交平台。SAU 投稿由 Rust 持久化、幂等防重并保留原 Python SAU 浏览器 sidecar，仍需真实平台逐账号验收。
- 逐 Python 端点矩阵位于 rust/docs/CONTRACT_MATRIX.md 和 .json，校验命令为 python3 rust/scripts/audit_contract_matrix.py --check。该矩阵刻意将所有完全等价验收设为 false。
- 禁止将这些局部兼容功能误称为完全迁移，也禁止将实验性 React 全页面预览与模拟服务结果用于宣布生产可切换。

### 原版 YouTube 来源及扫描控制（隔离）

- `/api/v1/youtube/sources` 原生提供来源创建/去重更新、查询、PATCH 和删除，保持 Python 来源 DTO、UUID 与状态字段；无识别结果的来源拒绝保存。
- `/api/v1/youtube/sources/{id}/scan/start` 先领取 PostgreSQL 扫描锁再返回 202，由 Tokio 后台任务实际调度 yt-dlp；同步扫描和 `/api/v1/youtube/scan` 返回源任务 ID / 工作流 ID，支持 limit 与 auto_process 选项。`since` 根据 yt-dlp 可用的发布时间过滤，发布时间缺失的条目在启用 `since` 时跳过；先枚举全部列表，再从未处理的视频中选取 `limit` 个，防止长播放列表遗漏旧条目。
- `/api/v1/health` 匿名可用且与 Python 返回体相同；YouTube 首页推荐扫描尚未由 V2 执行，设置页只读显示 Python 历史统计并保持 `home_scan_running=false`。

### Python AI 费用与存储设置（Rust V1 隔离）

- `GET/PUT /api/v1/settings/storage`、`GET/PUT /api/v1/operations/ai-usage/pricing`、`GET /api/v1/operations/ai-usage` 已接入原生 Rust SQLx。读取原版 app_settings 作为只读回退；修改直接写入原版 app_settings（生产环境尚未部署）。
- 费用报表按真实 `ai_usage_events` 聚合请求、错误码、Tokens、provider/model 与美元估算；数据库记录的实际费用优先，未定价请求保留 `null`。Rust 原生翻译、RAG 研究 Agent、投稿 AI 审核已通过 `ai_usage.rs` 写入每次模型 HTTP 尝试的成功/失败、用量和估算价格；Whisper ASR 和 Index-Translate 已额外接入逐请求事件写入（若模型未提供 Token 用量则记录 0，不虚构）；Embedding 及外部 Sidecar 仍需继续接入，不能宣称全部覆盖。
- 原版 `asset_ttl_days=0` 表示不自动清理已发布媒体。Rust Cleanup 在 0 时保留发布成品，非零时仅对已核实发布的 Rust 资产安排按天数延迟清理，并保留引用和未知投稿保护。历史 Rust 工作流且超过 TTL 的已发布资产还会保守补扫，检查未知投稿/P2/SAU 副作用和引用，原 Python V1 历史磁盘孤儿文件不在该清理范围。
- 测试：`rust/tests/operations_v1_isolated.py` 验证旧值只读、配置覆盖、单位计价、费用聚合、权限及输入校验。隔离测试 PostgreSQL 端口可通过 `VIDEOROLL_V2_TEST_DB_PORT` 调整。

### YouDub 与渲染管理 V1 局部兼容

- `/api/v1/settings/dubbing/jobs/summary` 从真实 Python `dubbing_jobs` 中统计 VoxCPM2 状态及最近任务；不启用模型，不判断 GPU 驻留是否就绪。
- `GET/PUT /api/v1/render-management/connection` 保持旧版连接地址字段及 Worker 协议版本，并将 Rust 更新保存到原版 `app_settings`。Worker、注册邀请与执行记录的 4 条只读管理接口也已映射 Python schema，包含真实心跳与设备字段；新增 7 条 Worker 控制端点（邀请码创建/撤销、Worker PATCH、设备 PATCH、凭证撤销、执行取消/重排），默认需显式启用 `VIDEOROLL_V2_ALLOW_V1_RENDER_CONTROL=true` 才能修改原版表。原 RenderWorker 端的实时心跳/领取仍属于外部服务；P2 的原版 DubbingJob 状态与 Python `outbox_events` 持久事件事务现已接通，并有同库差分测试；真实 Broker 交付、故障注入及在线设备签收仍待验证。
- `rust/tests/management_v1_isolated.py`、`rust/tests/render_management_read_v1_isolated.py` 和 `rust/tests/operations_v1_isolated.py` 使用隔离数据库校验 Worker 状态、执行关系、费用、资源回收及配置回退，不访问生产账号或 GPU。
- 字幕管理新增 3 条 `/api/v1/subtitle/auto/profile` GET/PUT 和 `/api/v1/subtitle/jobs/{job_id}` GET；前者只有 Rust 真正支持的配置可编辑，原 Python 未迁移字段将直接拒绝，后者读取真实字幕任务与资产。本阶段接口矩阵为 **93/217 部分适配、0 全面签收**，后续新增字幕质量和翻译上下文只读查询，最新总数见下面说明。
- `rust/tests/python_rust_subtitle_render_diff.py` 直接调用原版 Python 管理实现，对同一临时 PostgreSQL 中同初值记录进行 Python/Rust 差分，检查 HTTP DTO、持久配置、租约归属、重排幂等和作业资产；`VIDEOROLL_V2_TEST_DIFF_ONLY=true` 可在隔离 smoke 脚本中只跑这组专项。生产 Python 表未在本次测试中修改。


### 2026-11-23：字幕诊断 + P2 Outbox + 原 TaskStatus 数据库兼容

- 字幕质量 `GET /api/v1/subtitle/tasks/{task_id}/quality` 和翻译上下文 `GET /api/v1/subtitle/tasks/{task_id}/translation-context` 直接复用真实 SubtitleJob 引用及 V2 同根文件。翻译上下文修改、批次重译等仍未迁移；文件访问强制防穿越/符号链接。
- P2 管理取消、重排按原 Python DubbingJob 和 Outbox 事务执行；确保 owner fence、重复取消幂等、重排恢复暂停/失败配音，事件只写入本次隔离 PostgreSQL 进行验证，不投递生产 Hatchet。
- Python 生产数据库的 task_status ENUM 存储 **小写枚举名**，HTTP API 继续沿用 TaskStatus.value 的大写结果。Rust SQL 必须遵守这个差异，禁止恢复旧的 `'CREATED'::task_status` 等无效强制转换；对应隔离数据库已改为与生产一致的枚举定义。
- 当前审计结果为 **95/217 部分原生映射，完整签收 0**；隔离跨进程差分/真实平台业务验收仍未全部完成。

### 2026-11-23：启动 Python 生产服务后的只读模型提供方迁移

- 生产 `videoroll.target` 恢复运行，Web/Orchestrator/Hatchet/Redis 及其他子服务启动；本机 `http://127.0.0.1:3001/` 与 `/api/health` 返回 200。没有修改 Python release 链接或切换生产 Rust。
- 新增 Rust 原生只读 `GET /api/v1/subtitle/translate/model-providers`：沿用原 `subtitle.translate` 设置、`subtitle.model_providers` 真实 Python Provider 列表，返回 `legacy`、`index_translate`、自定义提供方模型项，隐藏 `api_key_enc`；增加模型 Token 预算与推理模式的校验。写入/删除/远程模型获取未迁移，不能默认宣称等价。
- 新增原版 Python `get_translate_settings()+list_providers()` 的同库差分夹具；但目前只有 `cargo check` 及 96 路由矩阵静态审计通过。因生产运行时内存与 Swap 压力大，完整 `cargo test` 编译超时，**未完成此新接口的动态差分验收**，不得计入完整签收。
- 当前矩阵 **96/217 部分原生映射，0 完整签收**；旧版模型目录变更、ASR/翻译设置以及原版字幕队列管理仍未完成。

### 2026-11-23：V1 翻译模型自定义提供方 CRUD（隔离开发）

- Rust 新增 `POST /api/v1/subtitle/translate/model-providers`、`PUT/DELETE /api/v1/subtitle/translate/model-providers/{provider_id}`，复刻 Python 自定义 Provider 的新增、修改、删除，以及字段校验、密钥保留/清空和 PostgreSQL 的 `subtitle.model_providers` 实体。该阶段内建提供方修改尚未迁移；后续已接通原版 `subtitle.translate`，详见下节。
- **安全开关默认关闭**：生产启动 Rust 也不会直接改 Python `app_settings`；只有已审核、隔离配置了 `VIDEOROLL_V2_ALLOW_V1_PROVIDER_WRITES=true` 的部署才允许写入。Python 原代码没有 Rust 的 advisory lock，跨两版本并行写同一行必须额外互斥。
- 使用 `dbplain:v1:` 兼容 Python 当前数据库凭据格式，明文从未出现在返回值。针对模型 Token/推理参数、重复 ID 和坏 URL 的保护与 Python 契约一致。运行时 Provider resolver 与管理接口共用原始 `app_settings`。
- 新增原版 Python 同数据库差分（创建、更新、删除、Token、密钥、无效请求拒绝）；静态 Rust 编译与专项差分已通过。**当前矩阵 99/217 部分原生映射，完整签收 0**，仍须真实平台和外部依赖验收。

- 全量隔离回归已验证 99 条接口探测与原版 Python 同库差分：58 个 2xx、40 个拒绝类 4xx、1 个因 B 站真实投稿被测试环境阻断的 502，0 个非预期测试失败。5 个详情读取路径仍缺真实测试行，不能作为业务等价签收。日志位于 `rust/target/v1-99-provider-crud-full-smoke.log`。

### 2026-11-23：内建 Provider 修改、受限远程模型目录

- Rust 内建 `PUT /api/v1/subtitle/translate/model-providers/legacy` 与 `/index_translate` 使用原 `subtitle.translate` 数据库 JSON，校验模型、API/Cerebras 参数和 Index 专属选项，测试集包含原 Python 函数返回与持久结果对照；如存在 V2 覆盖层，拒绝造成“保存成功但不生效”的写入。
- 新增 `GET /api/v1/subtitle/translate/model-providers/{provider_id}/remote-models`，读取 OpenAI 兼容 `/models`，无密钥回显。仅通过公网 HTTPS 和经过 DNS/IP 检查的固定目标，不跟随重定向，不走代理，限制响应大小。专用本机 HTTP Mock 要显式 `VIDEOROLL_V2_ALLOW_TEST_MODEL_CATALOG_LOOPBACK=true`；生产不要启用。当前不支持未经授权的内网网关模型目录获取。
- 内建 Provider 修改与远程目录已通过 Python 源码同库 + 本机 Mock 差分。**100/217 条部分适配，完整业务签收仍为 0**。
- 100 接口全量隔离 smoke 和原版 Python 源码差分均通过，日志 `rust/target/v1-100-provider-full-smoke.log`；58 条 2xx、41 条 4xx、1 条外部投稿受限 502、0 个非预期测试失败，尚无真实平台签收。既有远程 Writer/ASR 管理和生产账号外部验收待完成。

### 2026-11-23：Python 原版 ASR/翻译高级设置与运行时配置

- 原生 `GET/PUT /api/v1/subtitle/asr/settings` 和 `GET/PUT /api/v1/subtitle/translate/settings`（总计新增 4 条）：直接与原 Python 记录使用相同的 `subtitle.asr` 和 `subtitle.translate` 持久化 JSON。翻译高级设置包含 OpenAI、Index、RAG/词典、Embedding、搜索与 Agent，共 57 个读取字段。所有配置/秘密更新均使用事务和原版凭据存储格式。
- 仅已授权的 Rust 部署配置 `VIDEOROLL_V2_ALLOW_V1_SUBTITLE_SETTINGS_WRITES=true` 才能修改 V1 数据；隔离脚本开启。配置更新不仅改变管理页面：Rust 内建 OpenAI Provider 和 External Whisper、Groq Whisper ASR 会在任务执行时读取最新 DB 设置，其余需独立运行时的 ASR 模式仍属于外部 Worker。
- 原版 Python ASR 与翻译设置函数的真实 SQL/HTTP 差分均通过；完整隔离 smoke 利用错误启动模型验证 External Whisper 新设置被实际 Worker 消费。**104/217 部分原生适配、0 完整生产签收**。Hatchet 队列控制、Groq 真云端、Cloudflare 专用执行、RAG 全模式和其他原 Python 接口尚未全部验收。
- 最新全链路隔离日志 `rust/target/v1-104-full-native-model-selection.log` 通过：运行中 Worker 能消费 API 新存的 External Whisper 模型和内建 OpenAI 兼容翻译模型，SRT 含指定测试译文，FFmpeg 硬字幕生成成功。104 条 API 探测 62 个成功、41 个拒绝、1 个外部投稿受限阻断，意外失败为 0；`rust/target/104-unit-regression.log` 记录 105 项 Rust 测试通过。

### 2026-11-23：迁移处理队列与并发领取

- 新增 `GET /api/v1/subtitle/task_queue`、`PUT /api/v1/subtitle/task_queue/settings`、`GET /api/v1/subtitle/task_queue/broker`：保留 Python 的真实 SubtitleJob、RenderJob、PipelineRun 和 YouTube 下载 Inbox 查询，另外展示 PostgreSQL V2 工作流的真实 pending/running/expired/awaiting 租约状态和阶段。避免仅从 `Task.status` 推导“正在处理”，不让 V2 在队列页面消失。
- 下载设置写回 Python 同名 `workflow.youtube_download_concurrency` 配置；`Scheduler::claim` 也必须真实读取并限制新领取的下载数（含当前有效的旧版 Inbox 下载数）。PostgreSQL advisory transaction lock 保证多个 Rust Worker 同时 claim 不突破 V2 自己的下载限额；独立 Python 下载 Supervisor 并不参与该锁，双版本同时生产运行仍需调度所有权迁移。
- Rust `broker` 页面呈现 `v2:<stage>` 的 PostgreSQL ready/running/awaiting 数据，明确不冒充 Redis/Celery 的 Unacked、Reserved、Worker 心跳。原版 Python 真实数据库查询差分、过期租约恢复及 1→2 并发领取测试通过。
- **107/217 路由部分适配，完整业务签收 0**；真实跨服务调度混用、完整历史实时进度和生产级故障注入后续迁移。
- 最新完整隔离日志 `rust/target/v1-107-full-isolated-queue-final.log`：退出码 0，107/107 请求执行，65 条 2xx、41 条 4xx、1 条真实投稿受隔离阻断，非预期失败 0；`rust/target/v1-107-rust-unit.log` 107 项 Rust 单元/独立测试通过。新增多线程真实 PG 领取测试、原 Python 队列数据差分与租约恢复测试均通过。之前有一轮远程 RenderWorker 测试未到终态，此次已通过，待重复稳定性回归。


### 2026-11-23：原生翻译上下文手动编辑与 Whisper 本地模型目录

- Rust `PUT /api/v1/subtitle/tasks/{task_id}/translation-context` 会真实修改原版 SubtitleJob 的上下文 JSON 资产指针，并用原 Python 术语/人物变更规则计算受影响的字幕行号。已通过与原 Python 代码同 DB/同文件树的值和副作用对照；已有 `GET` 能回读新上下文。空上下文、任务不存在均拒绝，防止无意义写入。
- Rust `GET /api/v1/subtitle/models` 和 `DELETE /api/v1/subtitle/models/{name}` 操作真实 `SUBTITLE_WHISPER_MODEL_DIR`，并与原 Python 在文件名、目录总字节数、隐藏文件、实际删除与错误响应方面差分通过。模型删除需要 `VIDEOROLL_V2_ALLOW_V1_SUBTITLE_SETTINGS_WRITES=true`；开发期间只用于隔离目录，不触碰线上模型。
- 当前 **110/217 部分映射、0 完整生产等价签收**；模型下载/上传、代理测试及 ASR 专用本地运行时仍未迁移。
- 验收：日志 `rust/target/v1-110-full-isolated-smoke-repeat.log` 退出码 0，110 条 API 探测 66 个 2xx、43 个 4xx、1 个隔离环境外部投稿阻断，意外失败 0。原 Python 同库 + 同目录差分及真文件修改测试通过；Rust 单元和独立测试 109 项通过。远程 RenderWorker 以前偶发的隔离测试目标错认已补任务 ID 断言，并清理测试 V2 队列污染；完整生产级验收仍为 0。


### 2026-11-23：原生模型 ZIP64 上传及代理连接测试

- `POST /api/v1/subtitle/models/proxy/test` 真实通过传入的代理或数据库中的 `subtitle.asr.model_download_proxy` 测试 Hugging Face 的 HTTP(S) 地址，不会在非法域名/本地地址上发送测试请求；原 Python httpx 与 Rust reqwest 的 200、403、错误返回和配置读取已通过独立模拟正向代理的真实请求差分。
- `POST /api/v1/subtitle/models/upload?name=...` 由 Rust 直接流式保存 ZIP，再用原生 `zip` 库兼容 ZIP64/Deflate/BZip2/LZMA 解码和校验，经过内容数量/体积/路径检查后将临时模型目录变成真实 `SUBTITLE_WHISPER_MODEL_DIR/{name}`；失败时不残留模型片段。已与 Python 原接口对照解压后的两个文件、ZIP64 和非法压缩条目，全部通过。模型写入要求 `VIDEOROLL_V2_ALLOW_V1_SUBTITLE_SETTINGS_WRITES=true`；隔离测试独立设置。
- 新增 `POST /api/v1/subtitle/models/download`：Rust 解析 faster-whisper、OpenVINO、Embedding 模型别名，调用 HF Hub 的模型文件清单 API 并流式下载实际文件至临时目录；可选原版 ASR 代理和 Token，`force` 在新快照完整后才原子切换。隔离 Python 原模型下载函数（mock Hub）和 Rust HTTP 模拟 Hub 的路径、文件与版本替换差分通过；缺失 revision 则保留旧模型且清理临时目录。
- 当前 **113/217 条部分接口适配、生产级完整签收 0**。公网上的 HF LFS/Xet/鉴权、异常断点续传、大型模型和本地 Whisper/OpenVINO 执行尚未核实；不声称有 `huggingface_hub.snapshot_download` 全部缓存/重试能力。
- 最终日志 `rust/target/v1-113-full-isolated-final.log`：退出码 0，113/113 请求已执行，67 条 2xx、45 条 4xx、1 条真实投稿因隔离环境被阻断，非预期失败 0。`rust/target/v1-113-rust-unit.log` 为 112 项 Rust 测试通过。Python 原版函数真实 DB/磁盘差分、ZIP64 解压、Hub Snapshot 多文件下载、替换与失败回退均通过；无生产部署。

### 2026-11-23：ASR 模型 API 诊断和连接测试

- 新增 `POST /api/v1/subtitle/asr/external/test`、`POST /api/v1/subtitle/asr/groq/test`、`POST /api/v1/subtitle/asr/cloudflare/test`：使用真实 16kHz WAV、FFmpeg FLAC 或 Base64 WAV 向配置的 ASR Provider 发请求，并解析真实结果。实际读取 `subtitle.asr` 的 API Key，External 的批量和 VAD 参数传入真实 multipart；400/422 字段兼容回退。
- Python 原测试函数与 Rust API 在隔离 HTTP 服务上对照 WAV/FLAC/JSON、Authorization、返回结果和异常响应。**116/217 条部分适配，生产级签收仍为 0**；不能把测试音频成功冒充长音频 Provider/本机 OpenVINO 完全迁移。
- 最终 `rust/target/v1-116-full-isolated-smoke.log` 回归退出码 0：116 个 API 探测请求，68 个成功响应、47 个预期 4xx、1 个隔离外部投稿受限的 502，0 非预期失败；`rust/target/v1-116-rust-unit.log` 共有 113 项 Rust 测试全部通过。独立字幕和 ASR Provider 差分使用隔离数据库及模拟 HTTP Provider，未触碰生产 API Key/账号。

### 2026-11-23：字幕词典真实数据库管理

- 原生迁移 `/api/v1/subtitle/dictionaries/sources`、`/entries` 的读取、更新、删除，`/lookup` 和 `/import-presets` 共 8 个操作。直接读写原版 PostgreSQL `translation_dictionary_sources/entries`。实际修改启停/优先级，Source 删除级联删除词条；Lookup 以原规则检索并在事务中为命中词条增加 `usage_count`、保存 `last_lookup_at`。
- 原 Python `dictionaries.py` 的来源/词条 CRUD、精确与模糊检索使用同一临时数据库、相互隔离的词典领域做完整副作用差分；字典导入和提升至知识库、真实 Agent 集成仍待后续实现。**124/217 部分原生适配，生产级全签收 0**。
- 最终隔离日志 `rust/target/v1-124-full-isolated-smoke.log`：退出码 0，124/124 API 探测执行，71 个 2xx、52 个预期 4xx、1 个被隔离真实投稿 502，非预期失败 0；11 个缺少有效对象/受限的探测不计完整签收。`rust/target/v1-124-rust-unit.log` 114 项 Rust 测试全部通过。

### 发布后追加 P2 与 SAU 社交投稿（隔离）

- 发布后补 P2：V1 `POST /tasks/{id}/publish_jobs/{publish_job_id}/append_p2`，原生 `p2-tick`、`worker` 会处理 V2-owned P2 状态机；作者稿件 edit 前必须持久化 fence，未知结果绝不自动提交第二次。原生字幕会保留 P1 CID 回执并按已核实的 P2 增加第二分 P 字幕同步。
- 社交投稿：`VIDEOROLL_V2_ENABLE_SAU_PUBLISH=true`、`VIDEOROLL_V2_SOCIAL_PUBLISHER_URL`、`VIDEOROLL_V2_SAU_STORAGE_ROOT`、服务 HMAC 密钥都需要明确配置。V2 media root 与 SAU storage root 必须指向同一文件树；不允许只配置平台开关就触发投稿。
- `social-tick` / 独立 `worker` 读取 PostgreSQL SAU 出站意图，经受限 HTTPS/localhost sidecar 调用外部浏览器。一次不可逆调用后无重试，无收据视为未知；原版 SAU `publish_jobs` 回执只读核对并写回独立 V2 状态。独立 V2 的 `publish_all` 限明确的社交平台，不冒充原版混合平台批次事务。
- 真实 Bilibili 与三 SAU 平台账号、登录会话、风控、投稿与重启恢复均未验收。未满足全部 Python 契约前不得升级主生产服务。

### 单一数据库事实源（原位替换方向）

原 Python 的 `app_settings`、`alert_events`、`tasks`、`pipeline_runs`、`operation_inbox` 现已成为 Rust 的真实存储表；任务标题也仅存于 `app_settings`。迁移脚本 20261123000019–20261123000023 在隔离库验证。`vr2_workflows`、`vr2_jobs` 名称目前仅作为过滤 Rust 引擎的可更新**视图**（不是表、无独立数据）供已开发的调度器复用。原业务历史 ID、幂等键及租约/外部不确定状态保留；生产尚未执行迁移，其他 V2 表仍须逐项清理。详情见 `docs/SCHEMA_UNIFICATION.md`。

### 旧 RenderWorker 数据原位统一（隔离测试完成）

原版 `render_workers`、`render_worker_enrollments` 和 `render_executions` 是 Rust/原版 Worker 共用的唯一物理记录表。脚本 `20261123000024_unify_render_workers.sql` 与 `20261123000025_unify_render_executions.sql` 迁移旧 UUID、凭据哈希、租约、Fence 与历史执行；`vr2_render_*` 只保留按 `runtime_engine` 隔离的可更新兼容视图。原版 V1 渲染管理端点只呈现原协议执行记录，避免原 Rust Stage Job 的 `render_job_id` 不适配 V1 DTO。生产库尚未执行迁移，完整上线前需要专门审计和停机切换。详情见 `docs/SCHEMA_UNIFICATION.md`。

### 配音与投稿意图回归原版作业表

前向迁移 `20261123000026` 至 `20261123000028` 把 YouDub 成品证明、B 站独立 P2 追加与 SAU 意图写到原版 `operation_inbox` 对应的 Rust 阶段记录上。原版 `dubbing_jobs` 和 `publish_jobs` 仍是实际配音和 SAU 浏览器投稿回执的唯一来源；不为 P2 建立假 B 站首发稿件，也不覆盖 `unknown` 状态。独立 `vr2_*` 物理表减少到 **12/24 已合并、12 张待处理**，兼容视图只是过渡查询层。详见 `docs/SCHEMA_UNIFICATION.md`。生产仍未迁移。

### 2026-11-23：单物理业务数据库统一（完成 24/24 张表的隔离结构验证）

前向迁移 `20261123000029`～`20261123000033` 消除剩余 12 张 `vr2_*` 物理表。原任务元数据、发布审核、下载进度、哔哩哔哩原生字幕同步、RAG 知识/研究、播控关联回收到 `tasks`、`operation_inbox`、`pipeline_runs`、`translation_knowledge_items`、`playout_asset_links`。原版没有相同能力的媒体 GC 引用、工作目录回收、事件审计、登录限流单独保留为 5 张公共辅助表。独立 PostgreSQL 迁移验证：**`vr2_*` 物理表 0 张、24/24 处理完成**；必要的兼容视图不是持久存储。

本批迁移利用“Rust 新版从未投入生产，旧 V2 影子表没有持久数据”这一限定。迁移 30–33 会在任何旧 V2 影子表非空时立即抛错、回滚，绝不会静默删历史记录。生产数据库没有执行本次 DDL。具体表映射、限制与独立验证见 `rust/docs/SCHEMA_UNIFICATION.md`。完整原版 API 等价签收并未因此自动提高，目前仍为 147/217 部分原生适配、0/217 生产级签收。
