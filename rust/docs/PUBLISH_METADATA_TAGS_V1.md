# B站自动投稿标签优先级（2026-11-25）

Python resolve_bilibili_publish_tags 的规则：任务生成标签优先于用户显式标签；两者合并去重（大小写不敏感），自动标签过滤 videoroll；外部来源自动标签另过滤“原创/首发/独家/自制/独家首发”，但用户显式标签原样保留。只有前两类均为空时，使用已认证创作中心 GET /x/vupre/web/tag/recommend，参数 subtype_id/title/description；过滤泛化推荐、videoroll、超长标签及外部来源误导标签。API 不可用或无有效结果时才使用“视频分享”。推荐标签不是 topic_id 或 mission_id。

Rust 本轮在自动 B站投稿批次 admission 前实现了以上标签合并、官方推荐查询与 fallback；标签在 batch metadata 中冻结，不在每次重试重新查询。bilibili.tags.<task> 从原 app_settings 读取任务生成标签。官方推荐请求复用原 Cookie 读取和固定 HTTPS member endpoint；仅在无 AI/显式有效标签时发起。活动 topic/mission 校验仍由 bili_activity::verify_remote 在 UPOS 上传前执行，公共活动目录 ID 不可冒充 mission_id；真实 mission_id 必须与创作中心返回的 topic 配对，活动资格规则不明确时 fail closed。

## 测试与限制

cargo check --locked 通过；2 个标签合并/过滤单元测试通过；隔离投稿 smoke 37 PASS，覆盖自动/手动投稿、原生批次、回执及 after_render 回归；cargo fmt --check、git diff --check 通过。

重要未完成项：Rust 当前没有 Python translation_postprocess 中 ai_service.generate_bilibili_tags 的原生调用与写入者。故本轮能读取既有 bilibili.tags.<task>，并完整处理显式标签、官方推荐及兜底，但不能宣称 AI 自动生成标签等价迁移。应在后续复用现有翻译 Provider/用量账本，在字幕后处理阶段生成并写入原 app_settings 记录；失败应持久化空列表而不是复用旧标签。自动投稿已把 draft 中的“视频分享”视为系统占位，而非用户显式标签；人工保存的明确“视频分享”仍需区分 provenance，当前旧 schema 无该标记，存在歧义。活动自动匹配/报名与 mission_id 生成未在本轮增加；已有安全校验只验证已提供 ID，不代表用户已报名或符合奖励资格。没有生产库写入、真实投稿、部署或推送。
