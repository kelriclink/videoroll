# 高级研究 Agent 第一阶段（2026-11-25）

基线 54e8fa9。参考 Python rag.py 的 pretranslation_rag_gate_openai，接入已有 Rust Agent 与原知识库，不增加业务表或迁移。

- 原 subtitle.translate 中 rag_enabled 与显式 rag_auto_discover_terms=true 可启动研究；未选专用原生研究模型时使用原 legacy OpenAI 配置。保留原生独立 research_enabled 路径。门控模式外部搜索必须显式允许，且已有配置好的搜索端点，不自动推测 SearXNG 路径。
- 视频样本级门控：模型返回候选 term/category/scope/priority/need_rag/need_search；只接受高价值类别、实际原文命中、合法作用域，去重后最多12项；本地精确高分术语取消外部搜索。空候选直接保留原检索上下文。不是原Python逐翻译block门控的完整等价。
- 动态 search_knowledge 复用原已审核知识精确检索及现有native知识检索，按ID去重，最多5项；不额外发起动态向量请求。门控模式查询必须含候选术语，外部搜索还需need_search。重复查询拒绝，原最大步数1..8（门控另1次请求）、web次数最多4、历史大小预算保留，新增整体10..900秒超时（默认120）；不是严格实际Token/费用预算。
- 研究开始、模型调用、检索及持久化前后检查Worker租约；尚非逐写事务内原子租约栅栏，仍需并发取消故障测试。HTTP关闭重定向，错误记录不包含原始上游异常。动态检索证据ID及有界内容进入研究结果、摘要和字幕元数据。成功研究按revision 2 fingerprint复用；原pipeline_runs研究字段记录门控计划、工具过程及结果。非阻断研究失败保留初始本地检索上下文；原配置的研究模型变化缓存身份仍需专项差分。
- 不自动学习或写入知识库，不将研究输出转为硬术语。尚缺逐block门控、并行子Agent、Wikipedia与网页正文、原搜索参数完整适配、原translation_agent_runs详情记录、动态词典学习和独立Token预算。

验证：cargo check --locked --offline --all-targets通过；189/189库测试通过。实际Rust Worker回环测试使用原配置启动门控，过滤虚构词和普通单位，动态读取原已审核Rust compiler词条并排除pending，最终元数据包含新证据ID；后续翻译故障重试不重放研究，术语修复、滚动记忆、原表和审核保护回归通过。无实际外部搜索、无生产写入、无真实投稿、未部署或推送，未重新执行217接口全量业务验收。
日志 target/rag-master-check.log、rag-master-worker.log、rag-master-lib.log。
