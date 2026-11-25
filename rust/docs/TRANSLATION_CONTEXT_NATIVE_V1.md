# 原翻译场景与记忆第一阶段（2026-11-25）

基线 6463faa。参考 Python translation_context.py、translation_memory.py 与 translation_quality.py。原字幕编号/时间戳保持不变。

- 按 ASR 停顿 p75/p90 自适应阈值、强/弱标点、24 条/90 秒场景上限规划批次；尊重场景边界与原默认批次大小，原生手动 batch_size 1..50。边界计算为毫秒精度，未完成所有浮点舍入边界的 Python 差分。
- OpenAI 与 Index-Translate 实际批次传入场景、前 3/后 2 条源字幕、已完成的前序译文及有界参考记忆，独立于现有 RAG 术语表预算，不将参考资料当作指令。每条参考文本最多 240 字符，最多 6 个记忆示例。
- 原 translation_memory_entries、原 UUIDv5 namespace 与 identity；NFKC + lowercase + 常见 casefold 差异处理，并非完整 Python Unicode casefold。精确 source_norm 匹配；同一任务可使用机器记忆，跨任务只接受 approved 且匹配语言/领域。已审核条目不被机器覆盖。新原生 stage 没有对应旧 subtitle_jobs 时 subtitle_job_id 保持 NULL，不伪造旧 ID。仅在全批次翻译成功后写回机器记忆，不写 mock/noop；原 operation_inbox 租约及工作流状态检查、事务保护。记忆读写失败作为优化降级，过期租约拒绝。
- 检查点 revision 4 绑定完整源字幕、批次上下文、前序翻译和记忆示例、模型/RAG 配置。旧 revision 3 不盲目复用。还未进行长视频进程中断的完整恢复差分；完成后记忆变化可能使重试重新生成批次。
- 原数字和相邻重复检测的基础实现作为非阻断 warning，持久化在 segments_json 的 translation_quality_issues，场景/批次计划也持久化。数字检测仅按 ASCII 数字组启发式；Python 的单位缩放/小数/符号/标识符细节、硬术语约束、定向修复、结构化滚动摘要、角色/术语/歧义记忆、模糊 SequenceMatcher、selective retranslation 接入尚未完成。不声称完整 translation_stage.py 等价。
- 第40个迁移为原表与原3个索引的幂等补建，生产原表已存在，无新 vr2 物理表；从简化旧结构与真实 DDL 均验证通过。完整真实生产数据恢复历史报告仍是39个迁移，未再次复制完整生产数据。

验证：cargo check --locked --offline --all-targets 通过；185/185库测试通过；实际 Rust worker 回环模拟 ASR+OpenAI 5条源字幕/3批次，验证场景边界、前序译文、同任务审核记忆保护、跨任务未审核/错误语言/领域排除、Python UUIDv5 一致及原表写入、数字和重复告警；真实生产 DDL + 合成历史数据40个迁移和接管专项通过。未调用真实模型、未修改生产库、未执行真实投稿，未重新运行217全量接口业务回归。Index上下文已接入且编译通过，本轮实际Worker专项使用OpenAI。
日志：target/translation-context-check-r2.log、translation-context-worker.log、translation-context-lib.log、translation-context-schema.log。
