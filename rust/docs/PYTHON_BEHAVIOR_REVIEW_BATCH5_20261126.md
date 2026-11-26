# Python 行为等价审计第五批

基线 2e9c4da。八方向并行源码对照；实现五组业务行为，保留原业务表、API 和第三方模型运行时。

- Thinking：对照 ai/client.py:275–330,513–690。恢复字幕翻译及修复请求的 SSE、普通 JSON 回退、OpenAI 明确拒绝 enable_thinking 后一次标准协议回退、Cerebras reasoning_format 与 token 字段；按原响应字节预算拒绝截断/错误 SSE，记录每次 HTTP 用量，不记录 reasoning。明确的上下文超限交给已有缩批流程。暂未恢复 UI 实时 Thinking delta 及全部 Agent 提供商思考配置。
- RAG：对照 subtitle_service/rag.py:1095–1250。分离查询 single-flight 和提供商准入锁，搜索/网页正文最多4路，Wikipedia 1路；跨任务正文缓存300秒，每次缓存命中前重新校验 URL/DNS 策略，实际请求仍经过原固定解析防 SSRF 网关。共享429冷却不因并发成功而清除。未完成原逐请求网络重试/统一 Agent 请求预算差分；旧串行锁版本不应与本版混合运行。
- YouDub：对照 dubbing/youdub_native.py:122–289、youdub_control.py:29–181、youdub_job.py:75–110。已确认成品快速路径仍验证源视频与字幕指纹，GPU 准入后重新核对 pin；恢复5秒原生阶段采样及单调进度、按需进程组取消、驻留启动/加载等待与 Linux pidfd 精确进程取消。保守拒绝历史无 pin 回执，外部进程未知运行状态仍需人工核对；驻留子进程树、崩溃/心跳直接取消后的自动恢复及音频-only/RenderWorker 交接尚未实测或完成。实际 TTS 模型未运行。
- B站简介：对照 bilibili_publisher/worker.py:669–675,745–763 和 bilibili_web_client.py:79–96。只有 HTTP 成功且结构化 code=21010、明确简介过长、无 aid/bvid 的拒绝才可缩为1600宽字符单位并清除 desc_v2，修改后重新审核并取得提交准入；仅一次修正提交，其他错误不重放。测试覆盖分类和缩短逻辑，尚未做完整投稿流程 HTTP 拒绝后修正的专项。
- 合集：对照 worker.py:166–386,1163–1171 与 bilibili_web_client.py:746–925。投稿前冻结作者、账号和凭据摘要（无明文凭据）；仅将确认回执写入原 operation_inbox result_json，45秒独立轮询、首次延迟300秒。写入前持久化 creating/attaching 意图，跨任务创建冲突保护及原子 CAS；不确定写入只读核对，最多97次尝试，绝不重放 archive POST。保留已有不同人工合集；镜像可公开进度到原 publish_jobs.response_json，不改变稿件状态。当前凭据轮换后保守转 unknown，历史无意图稿件不自动补建；合集 step 真实隔离测试通过，但完整 tick 调度、原生产 schema 和真实平台仍待验收。

验收：cargo test --lib --locked 248/248；cargo check --bin videoroll-v2 --locked 通过（bili_collection.rs 一处未使用 Row 导入警告）；实际独立 PostgreSQL 三专项通过：Thinking HTTP、RAG 八请求峰值4及共享冷却、合集创建/加入各一次且模拟503后只读确认。专项与库测试存在重合。Python 原参考测试49/49（Thinking11、合集与限流24、YouDub14）。未安装生产依赖、未部署、未调用真实平台、未修改生产数据库。保留先前未提交的 schema rehearsal 测试文件，不混入本轮提交。
