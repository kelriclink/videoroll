# RAG 待审核知识学习（2026-11-25）

基线 3e731c3。原 rag_auto_learn_terms 显式启用时，门控并行研究全部成功后，允许从原已审核知识证据生成 pending 候选；不改变原数据库表或API。仅原文命中的global范围 proper_noun/acronym/domain_jargon/technical_standard，排除空译法、非法控制字符、无证据、任务及系列级语境。仅接受子Agent实际检索过的原知识ID，写入时重新核查语言、领域、审核状态及术语关联；外部网页证据暂不作为学习准入。证据关联不等于译法正确。

复用原Rust人工写入归一化及同一事务 advisory lock；非阻塞尝试，竞争时跳过。检查同领域或通用领域已有同名记录，绝不自动覆盖任何状态；其他领域独立。原唯一索引与 ON CONFLICT DO NOTHING 作为并发保护，稳定UUIDv5，confidence=0、status=pending、created_by=agent、无向量和last_verified_at，不宣称模型已验证。来源记录原知识ID、子Agent及任务，子Agent knowledge_item_id和学习事件、父研究结果同一租约事务提交。待审核项不会成为原已审核检索证据。未增加表或迁移，原研究指纹包含显式学习设置，关闭时保留序列化兼容。原串行路径未接入。

验证：全目标编译通过；195/195库测试；实际隔离Worker启用学习测试复刻原唯一索引，追加检索后模型503及后续翻译重试，最终恰好新增一个pending候选，原人工批准词条未变、证据ID及子Agent关联正确，未将新pending ID加入检索证据。关闭学习的多步Worker回归未新增知识。测试用模拟模型与本地知识，未执行真实外部请求。待补独立验证器、外部证据准入、审核API/前端完整业务验收、向量生成、并发人工编辑/强杀故障注入；现有pending候选不自动更新，不能声称完整原Python自动批准等价。未执行完整217接口验收，未修改生产库、部署或push。

日志 target/rag-learning-check.log、rag-learning-worker.log、rag-learning-disabled.log、rag-learning-lib.log。
