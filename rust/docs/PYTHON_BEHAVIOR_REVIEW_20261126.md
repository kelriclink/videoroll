# Python 行为等价专项审计（第一批，2026-11-26）

基线：rewrite/rust-v2 564fb14。范围为 git 管理的 src/videoroll 第一方 Python 代码，201 文件、65413 行、2710 个函数/类定义。AST 清单只是覆盖索引，不是语义签收。第一批重点检查用户指定的十个文件（24334 行、742 定义），逐文件浏览函数目录、核心实现和 Rust 对应路径；并未逐行签收所有定义。第三方 vendored 项目、测试和桥接脚本不计入此统计；桥接脚本的运行时差异单独记录。

状态：GAP=已看到明确差异；PARTIAL=存在替代实现但尚无行为等价验收；PRESENT=读到对应实现，不代表所有边界测试已通过；REVIEW=需进一步验证，不能直接算缺失。

## 第一批实质差异清单

| 编号 | 优先级 | Python 证据 | Rust 证据 | 状态与修复要求 |
|---|---|---|---|---|
| ASR-01 | P0 | processing.py:1629–1846（重叠拼接） | media/asr.rs 分片 midpoint 丢弃、简单 dedup | GAP：工作树已有 asr_overlap.rs 候选与独立测试；须全工程回归；保留真实重复语句和轻微碰撞，不改原导入字幕 |
| ASR-02 | P0 | processing.py:1390–1423、1849–2076、2459–2493；worker.py:554–589 | media/asr.rs:840–950、1388–1402、固定 WINDOW=45/OVERLAP=4、三次通用重试 | GAP：Groq 原45s/5s、最多5次重试、temperature=0、可取消全局限流、429共享冷却、Retry-After、Silero预筛与空窗口检查点。与现有 External Whisper 会话锁区分；保持已成功分片可恢复 |
| ASR-03 | P1 | processing.py:2555–2690；worker.py:626 | scripts/native/local_inference.py:46–94；media/asr.rs | GAP：原 PCM/Silero 实际语音边界校正和碰撞恢复未见完整替代，当前 bridge VAD 不能等同于后置时间轴校正。验证语音、音乐、静音、短句、双语字幕 |
| ASR-04 | P1 | processing.py:827–1051、1083–1258、2926–3084 | media/asr.rs:453–732、158–214；scripts/native/local_inference.py | PARTIAL：已有外部词时间戳 regroup、异常置信过滤和无语音结果；需核对动态语义切分、短句合并、多种语言、混合有词/无词时间戳、幻觉过滤。不可因缺语音一律阻断投稿 |
| TR-01 | P0 | processing.py:3223–3247、3304–3368、3535–3644 | media/asr.rs:1049–1175、2289–2416；providers.rs | GAP：通用 OpenAI 翻译尚未消费 context_window 做输入预算，没有缺失索引定向补译、成功前缀检查点及按需减半缩批。已有质量修复与固定批次重试，Index Translate 另有拆批，不能混称完全缺失 |
| TR-02 | P1 | ai/client.py:513–687；processing.py thinking callbacks | media/asr.rs translate_batch；ai_usage.rs；provider config | PARTIAL：Rust 有 enable_thinking 参数及用量记录；通用翻译走 chat_json 非流式，旧 SSE thinking delta、字段不兼容降级需逐项验证 |
| TR-03 | P1 | processing.py:3159–3220、3460–3530；subtitle_service/main.py:2256–2450 | media/translation_context.rs、translation_quality.rs、subtitle_context_write_v1.rs | PRESENT：场景规划、上下文学习、硬术语、受影响索引已找到对应路径；细查边界、删除术语、缺失文件与选择性重译，不能重复实现 |
| RAG-01 | P1 | rag.py:1095–1250；调用处2803、2843、3057、3168、3280、3372 | agent.rs:68–150；agent_tools.rs:104–280；agent_parallel.rs shared budget ledger | GAP：跨任务共享的 provider concurrency、429冷却/熔断、TTL HTTP 响应缓存（搜索60s、网页300s、Wiki 900/3600s）。已有任务内工具回执、预算、进程内 Wiki mutex，不能等同于跨进程提供商准入 |
| RAG-02 | P1 | rag.py:1251–2037、2292–2571、3628–4107、4187–4294、4585–6424 | agent_parallel.rs、agent.rs、agent_tools.rs、rag_original_v1.rs、rag.rs | PARTIAL：检查点、外部证据验证、知识检索、工具预算和并行 agent 已有原生对应，仍需逐项对比工具可取消性、恢复后的预算、检索阶段、证据降级、自动学习准入，不能仅按文档宣称等价 |
| DICT-01 | P1 | dictionaries.py:89–97、998–1072 | subtitle_dictionary_v1.rs:345–424 | PRESENT：精确/模糊搜索、语言/领域、优先级、质量、使用计数更新已实现；逐项核对规范化及边界输入 clamp/validation 差异 |
| DICT-02 | P1 | dictionaries.py:207–483、685–776；main.py:1397–1489 | subtitle_dictionary_parse_v1.rs、subtitle_dictionary_import_v1.rs:383–526 | PARTIAL：流式多格式、来源许可、SHA256、批次、replace/upsert 均有对应；失败导入收据与部分提交/全事务回滚策略、250k/无限制导入内存及失败回滚需验收。不要照搬旧版可能遗留部分数据的实现 |
| BILI-01 | P0 | bilibili_web_client.py:444–590；worker.py:892–900 | bilibili.rs:670–777；bilibili_settings_v1.rs:135–185 | GAP：设置 upload_workers 可保存但实际仍串行 PUT；旧版小于4块顺序，其他并发上传、完成列表排序、字节校验和实时进度。Rust 迁移须限制总 in-flight bytes，按块流式读取，取消检查，重试仅幂等 PUT；不得并发重复 archive POST |
| BILI-02 | P0 | bilibili_publisher/worker.py:435–610、646–652、873、1058 | bilibili.rs:1036–1182 | GAP：按积压量动态预留 upload / submit 时间槽，分别45+15(n−1) capped180、20+8(n−1) capped90，以及共享冷却。迁移时用原数据库可持久协调机制，不依赖 Redis/旧业务调度器；不要把不明确的提交结果重试 |
| BILI-03 | P1 | worker.py:195–390；bilibili_web_client.py:746–922 | bilibili.rs、bili_activity.rs、bilibili_publish_v1.rs | GAP：未找到按 YouTube 上传者匹配/创建 B站合集、补充合集封面、关联已发布稿件的完整 Rust 执行路径。以已确认稿件为前提，独立后置事件，不影响主投稿结果；不允许无回执重发 |
| BILI-04 | P1 | worker.py:653–677、732–765、1111 | bilibili.rs:778–815；bilibili_publish_v1.rs:729–787 | GAP：已确认投稿最终使用的实际 meta 与原预提交快照不同，Rust direct 确认更新未写最终 meta；原明确简介超长拒绝时缩短描述并清空 desc_v2 的特殊修复也未对应。仅在可证明未发生外部副作用时才允许改正后提交，其他情况 unknown |
| YT-01 | P0 | youtube_downloader.py:714–764、840–894 | download_runtime.rs:114–163、212–294；ingest.rs:91–210 | GAP：原 aria2/native 独立工作目录与故障降级、仅格式不可用时切换 extractor clients，Rust 当前单路径+通用重试。须保留独立续传文件、禁止跨 downloader 复用片段；已存在版本校验和协议限制继续保留 |
| YT-02 | P1 | youtube_downloader.py:154–228；youtube_service.py:425–437、675–735 | download_runtime.rs:212–294 | GAP：旧版有脱敏 extractor 诊断、失败日志、代理/bot提示；Rust stderr 丢弃导致失败根因不可见。应有受限字节/行数的脱敏日志，禁止记录 Cookie/代理凭证/签名 |
| YT-03 | P0 | youtube_service.py:692–735；storage/recovery.py:73–133 | ingest.rs:91–210 | GAP/REVIEW：Rust 对已登记原视频只检查非空文件，注释声称完整性而实际没有核对已有 hash/ffprobe；旧版遇到丢失对象会处理未完成图。不要直接复制 Python 清空投稿/资源记录；应按源哈希和 generation 隔离旧衍生资产，并保护所有历史发布结果 |
| YT-04 | P1 | youtube_downloader.py:303–493、494–631；youtube_service.py:378–425 | youtube_meta.rs、media/youtube_subtitles.rs、ingest.rs | PARTIAL：原元信息/字幕选择已有独立 Rust 路径；需验证自动下载后元信息、缩略图、标题/简介及资产事务与手动获取的一致性 |
| RENDER-01 | P0 | render_worker/runtime.py:577–677 | native/windows-render-worker/src/worker.rs:536–736 | GAP：完整已渲染成品缓存（不是字幕 PNG 事件缓存）缺失。旧语义指纹排除 fence/执行URL，输入校验、原子 rename、manifest SHA/size，72h TTL。Rust 失败时删除执行目录，可能导致大视频重复编码 |
| RENDER-02 | P0 | render_worker/runtime.py:678–720 | native/windows-render-worker/src/api.rs:116–144；worker.rs:688–736 | GAP：旧节点上传前先查询已登记输出，网络/5xx失败后查回执再重试同一成品；Windows 单次上传后失败。Rust 服务端恢复 API 已迁移，客户端应使用 fence+回执+成品缓存，不把409/410当普通重试 |
| RENDER-03 | P1 | render_worker/runtime.py:322–399 | native/windows-render-worker/src/worker.rs:473–512 | PARTIAL：设备按 codec、活跃负载排序已实现；原设备编码故障隔离/冷却与重新探测未见同等实现，需要物理节点测试 |
| WORKER-01 | P1 | subtitle_service/worker.py:646–793、972–985、1301–1338 | media/asr.rs、scheduler.rs、executor.rs、maintenance_workdir_v1.rs | PARTIAL：原日志上传失败不阻断、停止检查、临时文件回收、按阶段 checkpoint 清理、错误分类需要逐行为比对。Rust 租约和取消机制已存在，不应该重新引入 Hatchet |
| SUBAPI-01 | P1 | subtitle_service/main.py:196–323、416–444、467–630、2031–2224、2531–2845 | subtitle model/dictionary/settings/queue modules、scheduler.rs | REVIEW：模型 zip 安全提取、上传归档、真实服务连通性测试、硬件探测、处理队列进度及 broker 信息语义。217接口覆盖不代替这些业务验收；旧 broker 特有字段允许明确退休 |

## 下一轮实施分组

1. ASR-01/02/03：重叠拼接、Groq provider 专项及 VAD 时间轴，使用旧 Python fixtures 做差分。
2. TR-01：真实上下文预算、遗漏索引补译、动态缩批、成功前缀持久化；不因一个批次重跑整视频。
3. BILI-01/02/04：限内存并行 PUT、进度、跨任务准入、最终 meta。先本地 UPOS/creator mock 验证分片和失败矩阵。
4. YT-01/02/03：分离下载后端 partial 与 format fallback，受限诊断、完整性、代际衍生资产安全。
5. RENDER-01/02/03：远程节点输出回执、可复用成品及硬件故障隔离，Windows 实机验证后启用。
6. RAG-01/02、DICT、BILI-03 等补齐；随后进入其余第一方模块逐定义审查。

## 覆盖纪律

十个重点文件均做了初轮源码和 Rust 对照，但 742 个定义未全部完成逐行语义签收。剩余 191 文件也不能因 AST 解析成功而视为完成。审查表采用源文件/行号/对应 Rust 入口/测试证据/状态，后续逐条签收；禁止把相似函数名、API映射或存在文档当作业务等价。保留原 DB、API、第三方服务，修正旧版缺陷而非机械复刻。无生产写入、无真实投稿或线上调用。当前工作树已有上一轮未提交 ASR 重叠修复和隔离验收脚本，本报告不宣称这些已合并。
