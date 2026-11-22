# Rust Index-Translate / RAG Agent 实施与限制

已经通过隔离测试：Index-Translate 独立客户端、官方结构的编号字幕、单条漏译的有限补译、RAG 任务级知识查询、模型驱动 search_knowledge 后 finish、数据库中的 Agent 状态及证据、翻译检查点的研究指纹。

此外，模型 Provider 必须按所选 ID 解析，缺少 Provider、模型或旧密钥时必须明确报错，不能隐藏回退。RAG 资料被标记为参考文本，Agent 仅允许受限工具调用。

目前未完整迁移的原版功能：多源网页搜索与词典策略、RAG 专业 Agent 群、术语表前端管理、pgvector ANN 检索、全面多模型上下文预算、原版 Fernet 旧凭证迁移、所有 ASR Provider，以及真实在线 Index-Translate 配额和长视频质量验收。

模拟请求通过不代表官方接口稳定可用；不应开启真实自动投稿进行验证。
