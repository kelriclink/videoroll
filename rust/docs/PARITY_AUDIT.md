# Rust API / 原版前端迁移差异审计

本报告由源码扫描生成。HTTP 路径相同不代表功能完成：请求/响应、鉴权、并发、外部副作用和前端实际行为必须逐项测试。

- 原版 Python FastAPI 装饰器：**217**（含 WebSocket、各独立服务和动态路由）。
- 原版 orchestrator：**132** 个（含 WebSocket）。
- Rust 静态 HTTP 路由候选：**80** 个（V2 不等于原版契约）。
- 无前缀方法+路径字面重合：**31** 个（仅重合，未经验收）。
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
