# RAG 独立工具收据和预算（2026-11-25）

基线 e367f1f。并行固定检索路径采用独立 knowledge_receipt、web_receipt，原 initial_evidence 汇总结构保留。调用前在 workflow/inbox 租约保护事务中分别预留 model_calls、knowledge_calls、web_calls 并记录开始事件；成功收据及事件同事务提交。知识查询上限为候选上限乘子任务尝试上限2，模型总预算保持 max_research_steps+2，网页总预算保持 max_web_searches。网页预算用尽沿用此前跳过策略，记录 skipped 事件；没有预留就不执行。工具失败不消耗尚未开始的模型调用额度。预留后崩溃仍消耗额度，远端成功但收据未提交时可能有限重试，不保证远端 exactly-once。

并行父子记录使用 checkpoint revision 2 及新稳定 UUID 命名空间；原历史记录保留，已完成的总体研究缓存可继续使用，旧未完成的并行父子记录不会自动转换为新预算账本。部署切换须停止旧 worker；生产尚未运行 Rust。未新增表或迁移。串行工具循环不改动。

验证：全目标编译通过，实际 Rust Worker 回环网页503故障注入通过：2个知识查询、2个网页尝试、3个模型请求，预算分别4、2、4；第一次网页失败后不重放知识查询，成功的子Agent及门控不重放；原无网页模型503并行回归通过。测试仅访问本地模拟模型及搜索接口，没有真实外部请求。尚未实现子Agent自主多步动作、全站限流、Token/费用预算，也没有测试进程硬杀/全部预算拒绝分支或217接口完整等价验收。无生产写入、部署或push。

日志：target/rag-tool-receipts-check.log、rag-tool-receipts-web.log、rag-tool-receipts-parallel.log、rag-tool-receipts-lib.log。
