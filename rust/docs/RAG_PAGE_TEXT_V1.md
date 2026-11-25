# RAG 受控网页纯文本读取（2026-11-25）

基线 677f9f1。新增原生 subtitle.rag.page_text_enabled（默认 false），并行研究且已有允许的配置搜索结果时，只读取首条结果，不接受模型提供 URL。复用 egress_gateway_v1 的 DNS 全地址校验、固定连接 IP、禁止代理与危险头；仅 HTTPS、标准端口、无查询串/凭据/片段，禁止重定向。现有编译期 egress-test-loopback 特性只用于原隔离回环夹具。12秒总体超时、500000字节、严格 UTF-8 text/plain、拒绝控制字符及截断内容，归一空白后最多5000字符。拒绝及不支持的类型保留原搜索摘要，不将其当作正文。非成功5xx/429的错误按既有有限研究重试处理；若响应类型已被 Egress 拒绝则保留摘要。

每研究最多2次正文尝试，独立 page_calls/page_budget；page_receipt 保存成功正文或 null 跳过结果，租约事务保护请求预留和结果。并行 checkpoint revision 5，旧未完成记录不自动转换，无新表。非全站限流，不保证远端 exactly-once。普通HTML解析未引入依赖，计划评估 MIT dom_smoothie（https://github.com/niklak/dom_smoothie），将解析与网络层分离，验证依赖及锁文件，不使用正则解析HTML。

验证：全目标编译通过；普通库194/194；实际Worker回环搜索503后提供内网URL，被拒绝且保存跳过收据，保留搜索摘要，原翻译质量和恢复通过。单独编译原测试特性执行1项回环测试，纯文本成功、HTML/重定向/超限拒绝。未测试实际外部站点、完整Worker正文成功路径或硬杀恢复，未运行217条全面业务等价验收。未写生产库、部署或push。

日志：target/rag-page-check.log、rag-page-worker.log、rag-page-lib.log、rag-page-loopback.log。
