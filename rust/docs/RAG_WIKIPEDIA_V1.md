# RAG Wikipedia 固定检索与摘要读取（2026-11-25）

基线 fa5d44c。参考原 Python rag.py fetch_wikipedia_evidence 的固定英文 Wikipedia API 与页 ID 去重，采用 MediaWiki generator=search + prop=extracts 的组合请求，不新增任意 URL 抓取能力。仅在并行门控研究且原 rag_wiki_enabled=true 时启用，对 need_search 的真实候选按优先级分配最多2次共享 HTTP 尝试（含失败及重试），与现有网页搜索预算独立。每次最多3条正文命名空间条目，纯文本导言每条1200字符、标题200字符，固定 https://en.wikipedia.org/?curid=ID 作为来源，过滤无效/缺失/空结果，忽略响应提供的任意链接。HTTP 禁用重定向，12秒超时，512KiB 流式体积限制，进程内互斥及200ms间隔；非跨进程限流。测试专用显式回环环境开关限定 127.0.0.1 /w/api.php，测试请求不带模型/搜索凭据。

原 translation_agent_runs.checkpoint 增加 wiki_receipt 和独立计数，当前 workflow/inbox 租约栅栏保护预留、收据和事件，已完成结果在后续模型失败时复用。预算耗尽沿用检索跳过策略。并行 checkpoint revision 4 和 parallel-v4 命名空间，旧未完成记录不自动转换；无新增表或迁移。关闭 Wiki 的设置序列化保持原研究指纹兼容。仍非原 Python 任意搜索词/多页读取/URL 正文抓取的完整等价；串行及模型自主调用 Wiki 未接入，未验证真实 Wikimedia 网络接口或全站限流。普通网页正文读取仍待实现。

验证：全目标编译通过；192/192库测试；实际 Rust Worker 回环三个专项通过：Wiki 成功后模型503（Wiki 1/2，模型4/6）、Wiki 首次503（Wiki 2/2，模型3/6），关闭 Wiki 的原多步研究（Wiki 0/0）；页ID来源、重复页过滤、原证据检查点和质量控制回归通过。未真实外部调用、未写生产库、未运行全部217条业务等价验收、未部署或推送。

日志：target/rag-wiki-check.log、rag-wiki-worker.log、rag-wiki-fault.log、rag-wiki-regression.log、rag-wiki-lib.log。
