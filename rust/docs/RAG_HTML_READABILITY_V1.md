# RAG HTML 正文提取（2026-11-25）

基线 806686b。按授权引入 MIT dom_smoothie =0.19.0，MSRV1.85，当前rustc1.100；从官方Cargo registry获取并锁定34个新增包，未主动升级其他依赖。Cargo registry提示0.20.0可用，本轮固定已核查API的0.19.0；尚未执行独立全量依赖漏洞审计。

保留已有默认关闭的 page_text_enabled、既有搜索结果首条来源、独立额度及租约收据，不扩展任意URL能力。沿用Egress HTTPS/标准端口/无查询串凭据片段/无重定向、DNS校验与固定IP、无代理、500000字节与12秒网络期限。允许text/plain、text/html、application/xhtml+xml，严格UTF-8；Readability Strict 单次策略、关闭JSON-LD、Formatted输出，最多8000元素，输入额外限制12000个小于号，正文至少140字符且最多5000字符。两个进程内解析许可，spawn_blocking 内持有许可直至退出，3秒调用等待超时后保留搜索摘要；超时不能强杀解析线程，非完整CPU沙箱。无脚本执行或网络请求，无HTML渲染输出。原证据字段和检查点版本保持不变，因此此前已成功/跳过的正文收据仍复用，不自动重新解析。非UTF-8、动态JS页面、短正文与复杂表格可能无法有效提取；内容仍为不可信研究参考。

验证：Cargo锁定全目标编译通过；普通库195/195，含正文、实体解码、导航/脚本/样式/页脚过滤、短文/非法UTF-8/超限拒绝；原编译期隔离回环特性1项HTTP夹具通过（HTML及纯文本成功、短HTML/重定向/超限拒绝）；实际Worker原私网搜索结果拒绝及保留原摘要、503恢复与字幕质量回归通过。未测试真实网页、完整Worker HTML成功链路、硬杀或全部217业务等价；未修改生产库、未部署或push。日志 target/rag-html-check.log、rag-html-lib.log、rag-html-loopback.log、rag-html-worker.log。
