# B站投稿活动智能匹配

开关：设置 → 模型服务 → **AI 智能参与 B站投稿活动**。数据库持久化键为
`bilibili.publish.ai_activity_enabled`（存储在 `bilibili.publish` 配置 JSON 内）。
默认值 **false**；适用于手动和自动 B站投稿，且**不覆盖**用户手动填写的
`topic_id`/`mission_id`。

## 行为

1. 在上传成功、正式 `add/v3` 投稿前使用已确定的分区 ID 与视频标题，
   请求创作中心 `/x/vupre/web/topic/type`，以及标题/标签关键词的
   `/x/vupre/web/topic/search`。分别最多 30、20 条，不做全站无限抓取。
2. 获取公开目录 `/x/activity/page/list` 前两页（至多 60 条），仅用作候选信息，
   **公开活动 `id` 不是 `mission_id`**。模型只见标题、简介、摘要和归一化候选，
   不会获取 B站 Cookie 或 AI Key。
3. 复用“自动处理”选中的 OpenAI 兼容模型，第一轮最多挑出三个候选项。
   重新从 B站 `topic/search` 核查所选话题的真实 `topic_id`/`mission_id`；
   对公开活动先通过 `/x/activity/subject/info?sid=` 核查时效和描述，再从创作中心
   查询是否存在同名且唯一的投稿话题和任务 ID。详情不可验证则不参与。
4. 第二轮 AI 根据活动详情、作品内容及原创/转载类型决定是否参加。所有提交的 ID
   均来自 B站接口并由后端校验，不采纳模型输出的数值 ID。
   转载稿件必须有明确允许转载的活动规则，禁止将转载稿件标记为原创。
5. 结果保存在稿件发布记录 `response_json.activity`，包含活动名称、ID、选择原因。
   AI 不可用、接口错误、资格不明确、候选不足或非 OpenAI 模型时跳过活动匹配，
   **保持原投稿行为**，不自动重复提交已经发出的 `add/v3` 请求。

## 边界

- 活动规则、账号参与资格和奖励资格未必能通过公开 API 完整核验；
  AI 的判断不是 B站的正式资格确认。
- `x/activity/page/list` 与创作中心未必一致；日期与奖励条件须以活动详情为准。
- 本功能不代替原有的幂等投稿、速率限制、稿件审核机制。
- 不支持把 Index-Translate 这类仅提供翻译接口的服务当作通用 JSON 选择模型；
  使用该模式会跳过活动匹配。

## 主要测试

`tests/test_bilibili_activity_selector.py` 和 `tests/test_bilibili_activity_settings.py`
验证 ID 映射、手动覆盖保护、时效过滤、失败降级、转载限制和默认开关行为。
