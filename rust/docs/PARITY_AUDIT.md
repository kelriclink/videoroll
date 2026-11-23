# Rust API / 原版前端迁移差异审计

本报告由源码扫描生成。HTTP 路径相同不代表功能完成：请求/响应、鉴权、并发、外部副作用和前端实际行为必须逐项测试。

2026-11-23：217 个原版装饰器中已有 147 个被登记为部分原生适配，剩余 70 个未登记；**完整生产等价签收仍为 0**。词典批量导入、知识项管理、OpenAI 兼容在线向量写入/重建、Embedding 模型管理均已有原生实现；已增加原版真实投稿批次回执查询、V2 自有任务封面规范化上传与退役 YouDub 410；真实 Local/OpenVINO 推理、知识文件导入、Agent 端到端链路、超大文件、断电恢复和生产账号仍未完成。

- 原版 Python FastAPI 装饰器：**217**（含 WebSocket、各独立服务和动态路由）。
- 原版 orchestrator：**132** 个（含 WebSocket）。
- Rust 静态 HTTP 路由候选：**104** 个（V2 不等于原版契约）。
- 无前缀方法+路径字面重合：**53** 个（仅重合，未经验收）。
- 前端旧 API 调用文本候选：**193** 处，**160** 种 URL；可能遗漏动态拼接。

## 原版子系统

| 子系统 | 端点装饰器数量 |
|---|---:|
| bilibili_publisher | 10 |
| egress_gateway | 2 |
| orchestrator_api | 132 |
| social_publisher | 9 |
| subtitle_service | 55 |
| youtube_ingest | 9 |

## 上线前需要的逐项验收

HTTP schema、状态码、权限与会话、外部平台幂等收据、重启恢复、资产引用、GC、真实前端调用、实际设备与真实平台测试。

仅当原生 Rust 逻辑及其契约测试完成才可标记为已迁移。未支持端点应返回明确错误，不可伪造成功。

精确源码清单：LEGACY_ROUTE_CATALOG.json、FRONTEND_API_REFERENCES.json；生成脚本 rust/scripts/audit_legacy_routes.py。
