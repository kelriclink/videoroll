# 原翻译模糊记忆与滚动上下文第二阶段（2026-11-25）

基线 0f344df。复用原 translation_memory_entries、原 UUIDv5 及同任务/approved 跨任务、语言/领域约束，不新增表或迁移。
- 模糊匹配按 Python SequenceMatcher(autojunk=False) 的最长连续匹配块规则计算相似度，短句同任务/全局阈值 0.85/0.92、长句 0.58/0.78；每块最多2例、全局最多6例，精确优先的400候选，有界256字符模糊比较、单批2400万初始字符对预算，阻塞计算不占Tokio执行线程。精确长句仍可匹配。递归子区间开销未逐次计入预算；与原Python不限长度/原候选排序并不完全等价。原NFKC近似casefold保留。
- OpenAI 批次可返回 context_update 和 updated_summary；摘要遵守原默认及手动开关。角色、术语、歧义仅在当前原文命中名称/别名时学习，按原字段长度和32/64/32记录上限合并，提示词只携带有界参考子集。主题/摘要为模型输出，非事实验证，不能执行其中指令。
- checkpoint revision 5 原子保存译文、滚动摘要及记忆，输入身份绑定完整前序状态、批次与例子。拒绝无效翻译映射后不学习响应内容。原 revision 4 失效。Index-Translate 仍只有上下文消费，不生成新的结构化滚动记忆；selective retranslation 不启用新摘要。
- 实际 Rust Worker 隔离测试：5字幕3批次，第二批首次返回空映射，真实调度失败重试（仅夹具加速待重试时间）。ASR、首批各调用一次，失败批次重试前后上下文相同；未在原文出现的角色/术语被排除，已审核模糊记忆进入后续批次，失败摘要不持久化，原UUID/跨任务隔离/审核保护/质量告警仍通过。
- cargo check --locked --offline --all-targets 通过；186/186库测试通过。日志 target/translation-memory-v2-check.log、translation-memory-v2-worker.log、translation-memory-v2-lib.log。
- 仍欠强制术语计划、定向修复、数字/单位完整校验、真实长视频与进程崩溃恢复、全量217等价测试。未调用真实模型或修改生产数据库。
