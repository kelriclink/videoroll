# 原翻译质量控制第一阶段（2026-11-25）

基线 58a4b6c。参考 Python translation_quality.py 与 processing.py 的翻译验证、术语计划及局部修复，不增加业务表或迁移。

- NFKC 后识别 ASCII 数字、符号、小数、千位分隔、常见英文与中文数量级（thousand/million/billion/trillion、万/亿/億/兆）。字符串十进制规范化避免浮点舍入；重复数字按集合比较、源端 ASCII 标识符连字符兼容目标端翻译。数字差异仍是非阻断 warning，不单独触发模型修复；不等同完整单位语义换算、Unicode 数字及 Python 正则/Decimal 的所有边界。
- 按原文出现的名称/别名生成逐行约束。显式 translate_glossary 字符串映射（新增原生配置扩展，最多64项，每项240字符）属于硬约束，来自自动profile或手动扁平字幕配置；滚动记忆术语与角色为 preferred，不升级为 hard。原 glossary_id 仍拒绝，未发现原Python主执行链完整的ID解析；Index自由文本glossary不按猜测分隔符变成硬约束。原 RAG term_cards 的结构化约束尚待接入；UI未增加此映射编辑功能。
- OpenAI批次：硬术语缺失、推荐术语缺失和相邻不同原文的重复译文可触发一次定向修复请求，仅携带受影响行、原草稿及约束/问题；要求返回完整受影响ID集合，拒绝额外、重复、空或遗漏的行。数字告警不进入修复理由。非阻断修复失败保留原稿，硬约束未满足则拒绝批次、不写检查点/记忆/最终字幕。重新校验修复结果，修复回复的上下文不学习。租约前后检查，AI用量独立记录。初次翻译缺失ID仍走既有阶段重试，并非Python的缺行补译/缩批完整实现。
- Index可消费计划及执行硬约束门槛，但尚未实现同等定向修复协议；selective retranslation的质量闭环、跨批次相邻重复的修复仍待完成（跨批次重复仍记录告警）。
- 检查点revision 6绑定当前约束，成功修复后的译文及滚动记忆进入原子检查点，旧revision 5不盲目复用；首轮失败重试行为保持。最终元数据保留数字、重复及未满足的preferred告警。模型质量告警不等同人审。
- 验证：cargo check --locked --offline --all-targets通过；188/188库测试通过，含200 million/2亿、千位/小数、标识符、全角及符号数字、重复数字、450W/4500W和hard/preferred区分。实际Rust Worker回环测试（5字幕、3批次）：首批缺失硬术语且重复译文，恰好1次局部修复；数字偏差保留为warning；第二批故意返回无效映射，实际调度重试复用ASR及已修复首批、原上下文/记忆/UUID保护。修复请求未跨批次，模拟模型未调用真实端点。未进行真实模型、完整长视频、所有错误修复返回变体或217接口全面等价验收。
日志：target/translation-quality-check.log、translation-quality-worker.log、translation-quality-lib.log。未写生产库、未部署或推送。
