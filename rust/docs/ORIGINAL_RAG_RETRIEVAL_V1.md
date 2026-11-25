# 原知识库 RAG 检索接入

基线 5fd6bd7。复用原 `translation_knowledge_items` 和 `subtitle.translate`，无需迁移或新增表。

- 原版 `rag_enabled` 独立控制旧知识库检索；原生 `subtitle.rag` 仍独立控制原生语料与研究 Agent。
- 有界英文单词/短语及中文候选精确匹配，优先已批准术语；pgvector 余弦相似度按 `target_lang`、领域（含无领域通用条目）、审批状态、精确模型键与维数、阈值及 top_k 查询。保护混合维数、零向量与任务私有知识。使用有界 SQL 查询超时，未增加 HNSW 索引。
- 向量请求失败时非阻断模式保留精确术语，阻断模式返回错误。原生向量失败时非阻断模式保留原生词法结果。
- 旧/新命中稳定去重、术语优先，在原有上下文预算内交给 OpenAI/Index-Translate；字幕段落元数据追加证据 ID 与摘要哈希，翻译检查点继续绑定上下文。

## 验证

- `cargo check --locked --offline --all-targets` 通过；库测试 180/180。
- `VIDEOROLL_V2_TEST_ORIGINAL_RAG_ONLY=true`：独立 PG、模拟在线 Embedding、混合模型/维数/语言/领域/状态、私有记录隔离、异常降级/阻断/关闭开关全部通过；真实本地 OpenVINO BGE 512 维检索进入翻译上下文。
- 完整 `smoke-isolated.sh` 退出 0：153 条 PASS 日志标记（54 条 PASS DIFF）；真实 Rust 工作流通过模拟 Index-Translate 并保存原精确术语、向量文档及原生知识的研究证据；字幕段落资产包含原知识 ID 和哈希。217 接口探测 125 正向、91 拒绝、1 隔离阻断、0 异常。
- 日志：`rust/target/original-rag-isolated.log`、`original-rag-full.log`、`original-rag-lib.log`。隔离 PG 卷已回收。

## 限制

这是原版精确/向量检索的实际接入，不是原 Python master Agent 的全部等价迁移。候选算法为有界近似、字幕采样与上下文预算不同；Agent 动态 search_knowledge 工具仍检索原生语料，旧版词典动态学习、研究子代理、门控与完整用量预算未接入。原知识库不再等于不可消费，但仍需长视频、4096 维真实 Qwen 服务及生产知识库检索性能验收。当前本地模型仍按请求加载。本轮未写生产数据库、未使用生产密钥、未启动生产服务、未真实投稿。
