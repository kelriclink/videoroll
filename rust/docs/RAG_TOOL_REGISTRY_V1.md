# RAG 共享检索工具第一阶段（2026-11-25）

基线 bffb58b。将串行 Agent 和并行子 Agent 重复的原知识库与 native 知识库查询合并为 agent_tools.rs。RetrievalKind 限定现有 Knowledge/Web 两类；统一非空、250 字符、控制字符校验及大小写/空白归一的查询身份。原知识库优先、ID 去重、最多5条、标题200/正文650字符，调用前后校验当前租约。Web 保留已有配置端点、外部搜索开关、调用预算和原 HTTP 执行器，未增加网络能力。门控和共享预算仍由各自调度器执行；并行初始检索检查点保持兼容。

验证：全目标编译通过；190/190库测试通过；实际 Rust Worker 串行及并行回环专项通过（原知识检索、503失败恢复、检索检查点、4/4共享模型请求、翻译质量修复及记忆）。网页搜索未启用，尚无独立 web 请求回归。该模块是共享类型化检索入口，不是完整自主多步 Tool Runtime；持久化逐工具预算、意图/收据、Wikipedia、正文抓取仍未完成。未执行217条完整接口回归。无生产库写入、实际外部调用、部署或push。

日志：target/rag-registry-check.log、rag-registry-parallel.log、rag-registry-serial.log、rag-registry-lib.log。
