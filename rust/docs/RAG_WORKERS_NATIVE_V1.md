# 原RAG并行子Agent第一阶段（2026-11-25）

基线 a452130。对照 Python rag.py _run_research_agents、agent_runtime.py；参考成熟实现的结构而不引入新的 Python 服务/工作流依赖：
- LangGraph 节点边界检查点：https://docs.langchain.com/oss/javascript/langgraph/thinking-in-langgraph
- OpenAI Agents SDK manager/specialist与工具guardrail：https://openai.github.io/openai-agents-python/multi_agent/ 与 https://openai.github.io/openai-agents-python/guardrails/
- Temporal 稳定操作ID、幂等及有界重试：https://docs.temporal.io/activity-definition 与 https://docs.temporal.io/design-patterns

原 rag_auto_discover_terms 启用且 rag_agent_parallelism>1 时进入并行模式，范围1..8；默认1与非门控模式保留已有串行研究流程。每个已过滤的候选术语由一个专门子Agent研究，最多 max_research_steps（1..8）个；每个子Agent精确查询原已审核术语、可选native知识，最多5条，按候选顺序确定性归并。外部搜索仅允许原开关、need_search与显式配置端点同时满足，固定候选术语查询，不允许任意URL或Shell。研究模型负责对单术语输出候选译法及理由，不能自行写库；模型输出仍为参考而非硬约束，不会自动学习。非阻断模式允许汇总部分成功结果；阻断模式任一失败则整个研究阶段重试，但已成功子任务不重放。

原 translation_agent_runs 保存 rag_master / rag_term_research 父子关系、checkpoint、result、状态、尝试次数；translation_agent_events 保存门控/尝试/汇总事件。无需新增表或迁移。稳定UUIDv5命名空间绑定工作流研究指纹与解析后的模型配置、候选术语；模型/端点变化使指纹更新；不存密钥明文。原 pipeline_runs 继续记录研究总体结果与master_agent_id。成功的门控及子结果跨调度重试复用。模型请求预算在父记录事务内预留，累计最多 max_research_steps+2（门控+候选+一次额外请求的共享上限），每子尝试最多2次；外部查询累计不超过max_web_searches。预留先于外部工作，取消或崩溃会消耗额度，未确认的远程请求可能被有限重试；不声称远端 exactly-once。不同研究指纹/不同任务独立预算；并非全站全局限流或严格Token/费用预算。

并行Future由父阶段持有、有界buffer_unordered，无后台脱离任务；同一原workflow/inbox当前租约行锁保护父子写入和预算分配，事务不跨HTTP；租约丢失拒绝写入，不能非阻断降级后继续翻译。总期限由原rag_agent_timeout_seconds控制。普通期限结束时标记仍在运行的父/子记录失败（前提租约仍有效），进程硬杀或取消后尚未有独立历史记录扫尾器，下一次合法执行可接管未完成记录。旧串行工具过程的全部写入尚非原子栅栏。并发上限是每个研究批次，不是全机共享并发。当前并行子Agent是一次固定工具检索+一次结构化模型判断，不等同原Python自主多步子Agent；Wikipedia/正文抓取、逐block门控、严格Token和费用预算、自动知识学习仍待完成。

验证：cargo check --locked --offline --all-targets最终通过；189/189库测试。实际Rust Worker通过 VIDEOROLL_TEST_PARALLEL_RAG=true 与 TRANSLATION_CONTEXT_ONLY 隔离测试：2个术语并发峰值2，一个503，真实调度重试只执行失败子Agent；门控1次、成功子1次、失败子2次，持久化共享预算4/4、web0/0，原父子历史/失败事件/证据及最终字幕通过；随后翻译失败重试也未重放研究。原串行模式单独重跑通过。没有真实外部模型或外部搜索调用、没有完整217接口验收；未测试强杀和并发取消、预算拒绝分支、真实全站并发。生产库未写入、未推送/部署。首次编译的Send生命周期问题通过有界显式boxed Future修复。

日志：target/rag-workers-check-r3.log、rag-workers-parallel.log、rag-workers-sequential.log、rag-workers-lib.log。
