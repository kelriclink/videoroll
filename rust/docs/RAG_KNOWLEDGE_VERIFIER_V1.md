# RAG 独立知识验证器第一阶段（2026-11-25）

基线 9609799。原 Python verify_glossary_entry_openai 的 supported/context_consistent/should_write/should_auto_approve/confidence 决策迁移为独立模型请求（复用当前研究 provider，并非不同模型的独立共识）。仅在原自动学习启用、所有并行子Agent成功、新全局候选符合学习条件且有原已审核知识证据时执行；现有同名词条直接跳过。严格结构化 JSON、置信度有限且0..1、证据ID必须来自实际提供记录、去重且最多5条。supported/context_consistent/should_write 均真、confidence>=0.7、非空证据引用才允许候选进入 pending；0.7为本阶段保守准入选择，不等同原Python完整自动批准策略。明确否决的映射不进入翻译推荐词列表；未触发验证或验证耗尽的映射仍可能作为任务级参考，不能写库，完整既有词条/术语冲突优先级尚待完善。自动批准建议只记录，不执行；候选 confidence=0、无向量、last_verified_at=NULL 保持人工审核边界。

父级独立 verification_calls/verification_budget（学习启用时2*max_research_steps，关闭为0），每子最多2次；请求前事务预留并保存事件，失败可独立有限重试，外部调用不持有事务。子Agent verification_receipt 包含候选/原文/语言/领域/实际已审核证据有界快照及更新时间的SHA256指纹；同指纹复用已完成验证，写入前在当前租约事务内重查证据并对原记录取共享锁，指纹变化则跳过；只保存验证器实际引用的合法证据ID。不保证远端exactly-once，证据内容相关性由模型判断，尚非事实证明。并行checkpoint revision6及新父ID命名空间；学习启用的总体研究指纹revision4，旧未完成记录不自动转换，关闭学习时已完成总体缓存仍可复用。无新表或迁移。

验证：全目标编译、195/195库测试；真实Rust Worker回环三专项：验收路径验证器首次503、第二次给出auto-approve仍只写pending、人工批准词条不变；否决路径首次伪造证据UUID被拒、第二次明确否决，不保存候选且移除推荐映射；关闭学习路径0次验证请求、无知识写入。两种学习路径验证预算2/4，原研究模型预算与多步恢复不变，后续翻译重试未重放验证。尚未测试低置信度独立边界、证据并发修改/锁竞争、验证期间进程硬杀、真实外部证据、完整217条业务等价；未生产写入、真实外部请求、部署或push。

日志 target/rag-verifier-check.log、rag-verifier-accept.log、rag-verifier-reject.log、rag-verifier-disabled.log、rag-verifier-lib.log。
