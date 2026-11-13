# 工作台 B站热点与活动

工作台首页新增「B站热点与活动」预览区；侧边栏「生产 → B站热点与活动」进入完整列表。

## 数据源与含义

- 热门视频：首选公开排行榜 `GET https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all`；
  当排行榜出现 HTTP 200 但 B站业务错误（如 `code=-352`）时使用
  `GET https://api.bilibili.com/x/web-interface/popular?pn=1&ps=20` 作为备用。
  界面明确显示实际来源（`排行榜` 或 `热门综合`），不会把热门综合冒充官方排行榜。
  按来源返回顺序展示至多 20 条，包含视频标题、UP 主、播放、点赞和跳转地址。
- 公开活动目录：`GET https://api.bilibili.com/x/activity/page/list?plat=1,3&pn=N&ps=30`。
  查询前两页，筛选状态为进行中且起止时间覆盖当前时刻的活动，最多展示 30 条。
  提供官方活动详情页链接和截止时间。
- **公开目录活动 ID 不是 `topic_id` 或 `mission_id`**。此页面只能用来浏览，不声明账号已报名，
  也不以活动列表 ID 直接参加投稿活动。实际资格请以 B站官方活动页及投稿规则为准。

## 接口与安全性

- 新增只读 API：`GET /bilibili/discovery`；`?refresh=true` 可手动刷新。
- 不调用投稿接口、不修改任务、不使用账户 Cookie。所有上游 URL 固定，超时及禁止重定向。
- 仅输出经过规范化的固定字段，不向前端透传 B站 API 原始数据。
- 视频页 BV 号校验；活动跳转必须使用 `https://www.bilibili.com/blackboard/`；
  图片仅使用 B站 `*.hdslb.com` CDN。
- 单进程内成功数据缓存约 5 分钟，手动刷新最短间隔 60 秒，降低上游调用频率。
  两类数据分别缓存，单类上游失败不会影响另一类；失败时最多保留 1 小时旧缓存并标注。
- 不创建任何自动任务，不自动报名，页面访问无需 B站登录。

## 验证

- `tests/test_bilibili_discovery.py`：字段解析、恶意地址过滤、失效活动过滤、
  分页去重、异常回退与缓存限流。
- `src/web/src/features/bilibili-discovery/bilibiliDiscovery.helpers.test.ts`：数值和时间显示。
- 前端 ESLint、Vitest 和 Vite 构建；通过只读真实 B站 API 取数验证。
