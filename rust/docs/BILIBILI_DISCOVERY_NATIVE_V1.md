# Rust原生 B站公开排行榜与活动工作台（2026-11-24）

本轮迁移原 Python GET /bilibili/discovery，保留后台登录保护，
直接调用 Rust 内置的官方 Bilibili HTTPS GET 固定端点，
不依赖旧 Python orchestrator 或 Sidecar，不读取投稿 Cookie，
也不写入 PostgreSQL。

实现：
- GET /bilibili/discovery?refresh=false|true
- ranking: 调用 https://api.bilibili.com/x/web-interface/ranking/v2，
  若非 200/非 JSON/返回错误代码或列表缺失，则回退
  https://api.bilibili.com/x/web-interface/popular。
- activities: 调用 https://api.bilibili.com/x/activity/page/list，
  固定 plat=1,3 和 pn=1/2，每页至多取 30 条；第二页失败保持第一页。
- 只保留合法 BVID、可接受的公开图片域名、B站官方活动链接；
  活动必须已开始、仍有效、状态有效。去重和字段长度同原版。
- 输出原版 ranking/activities/note 三层 JSON，排行榜最多 20 条，
  活动最多 30 条，保留各自 source、fetched_at、stale、error 状态。
- 成功数据进程内缓存 300 秒；手动 refresh 也有 60 秒节流。
  若远程失败但之前成功数据不足 3600 秒则退回标记 stale 的缓存；
  否则给出原中文错误，不阻断工作台。
- 公开目录活动 id 仅是公开活动列表 ID，不转换/假设成 topic_id
  或 mission_id，更不推断账号已报名或具备投稿资格。
- 请求只允许固定 Bilibili 官方 URL，禁止自动 30x 跳转，
  限制连接 4 秒、请求 8 秒、响应最多 1 MiB。
- 仅在专用隔离环境变量组合下允许固定本机 HTTP 18196 模拟站点；
  测试脚本为所有隔离运行提供此模拟目标，确保全量回归不会访问真实 B站。

验证：
VIDEOROLL_V2_TEST_BILI_DISCOVERY_ONLY=true bash rust/scripts/smoke-isolated.sh

使用临时 PostgreSQL、真正 Rust HTTP 接口和本地模拟 B站服务，直接
对比 Python 原版 parse_ranking/parse_activities 的返回值，
包括重复记录、非法 BVID/URL、公开活动时段和资格字段。
额外检查缓存/刷新节流、-352 排行接口回退热门、活动第二页失败
保持第一页，以及全部上游不可用时原样退化为空结果。

该路径计入路由部分适配；没有运行真实 B站网络查询，实际响应
变化、工作台/UI 全流程和生产故障恢复尚未签收。
