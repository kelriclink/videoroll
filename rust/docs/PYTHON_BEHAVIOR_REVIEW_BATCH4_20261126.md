# Python 行为等价审计第四批

基线 d19c74c。八方向并行源码对照，五个功能专项及后续四组补强；不改变原业务 API 或新建影子业务表。

| 方向 | Python 证据 | 已实现与边界 |
|---|---|---|
| 标题 | publish_meta_draft.py:48–112,185–241; index_translate.py:90–124; ai/service.py:293–337 | 按原 auto_profile/translate 配置选择 OpenAI 或 Index，已有译名和 CJK 直接复用，无凭据的旧 OpenAI 保留原文；严格 JSON 单行有界标题，带摘要失败后去摘要重试。原标题来源覆盖仍遵循已有已保存草稿优先级。仅 B 站自动投稿缺省草稿也接入该流程，通用标签回退不阻塞推荐。开放三个相应 profile 编辑字段。真实 mock 仅验 OpenAI 标题请求，Index 真实网关、单平台全链路待验。 |
| RAG | rag.py:1061–1250,2735–3379 | 搜索和 Wikipedia 原 app_settings 有界共享 TTL 缓存及 429/连续失败熔断；单独 PostgreSQL 会话持有提供商锁，短事务记录 128 条证据及最多32条提供商健康状态；两独立连接池验证单次请求、缓存读取、共享熔断及租约取消。当前保守每提供商串行，不是原搜索4并发；仍缺正文共享缓存、网络重试和真实多进程部署验收。 |
| ASR 边界 | processing.py:2555–2690 | 窗口级 Silero 语音跨度修正，不合并文本，偏移最多450ms、冲突时退回已知安全原时间；不同于原逐句窗口推理，保留跨窗口语义拼接；ASR checkpoint revision 4，已有 revision 3 会失效。实际 CPU Silero 合成语音验证，未作真实视频逐字幕差分。CPU VAD 独立保留1个轻量会话、60秒空闲回收，不挤掉2个原模型池槽位。 |
| B站 | worker.py:565–655; bilibili_web_client.py:195–229 | 识别平台601/406/-702及频率提示和HTTP429；分上传/提交更新共享冷却，上传60–180秒、提交20–90秒。分片429停止本次快速重试；明确限流仍不自动重放稿件POST，当前失败恢复遵循原未知回执保护。未完成合集异步状态机，真实平台未调用。 |
| 播控 | playout_service.py:174–310 | 原已就绪 Python 链接按相同资产/频道、SHA256、原相对路径校验并返回，可恢复缺失文件且不覆盖；支持原嵌套路径，拒绝逃逸和符号链接，旧 API 列表显示原物理表链接。仍仅允许当前 Rust 拥有任务；未修改真实旧文件，历史链接实际端到端隔离测试待完成。 |
| 配音、合集 | youdub_native.py:153–289; youdub_job.py; bilibili_publisher/worker.py:195–386,1163–1171; bilibili_web_client.py:746–925 | 再审查：native 配音目前仍以 preparing/0 或 succeeded/100 投影，resident/进程控制、混音及对齐字幕后置渲染未实现。合集须独立持久任务、已发布证据、账号绑定、存在性检查和未知外部回执隔离，不重放 archive POST；本轮未修改这两块状态机。 |

验收：cargo test --lib --locked 242/242；cargo check --bin videoroll-v2 --locked 通过。三个独立 PostgreSQL 隔离专项（RAG共享缓存、标题 HTTP 模拟、投稿冷却）均通过；专项与库测试存在重合。Python 原参考测试 90/90（标题/限流17、RAG44、Groq/可读性29）；使用现存依赖与隔离测试配置，未安装包。实际安装的 CPU Silero 检测 FFmpeg 合成语音通过。生产数据库、实际投稿、服务部署、最新 Release 编译均未执行。
