# SAU 独立服务 → Rust 控制面契约映射（2026-11-23）

本表只说明**Rust 编排层如何调用原 Python SAU sidecar**。它不是“9 条 SAU FastAPI 路由已用 Rust 重新实现”的声明。Playwright 登录、storage_state 加密、账号检查、Celery 浏览器投稿和原 `publish_jobs` 回执仍由独立服务及原表负责。迁移目标是原前端请求兼容、不可重复投稿、无密钥泄露。

| 独立 SAU 原接口 | Rust V1 兼容入口（路径统一加 `/api/v1`） | 处理方式 |
| --- | --- | --- |
| `GET /health` | `GET /settings/publish/social/health` | HMAC 代理原服务，返回 sidecar 状态，不混同 Rust `/ready` |
| `GET /accounts?platform=` | `GET /settings/publish/social/accounts?platform=` | 代理原账号列表 |
| `POST /accounts/{platform}` | `POST /settings/publish/social/accounts/{platform}` | 验证 multipart、平台和 storage_state JSON 后转交服务 |
| `POST /accounts/{account_id}/check` | `POST /settings/publish/social/accounts/{id}/check` | 账号检查保留原 SAU 的异步作业 |
| `DELETE /accounts/{account_id}` | `DELETE /settings/publish/social/accounts/{id}` | 交给原 SAU 禁用账号，沿用忙碌冲突检查 |
| `POST /login-sessions/{platform}` | `POST /settings/publish/social/login-sessions/{platform}` | 创建原浏览器登录会话 |
| `GET /login-sessions/{session_id}` | `GET /settings/publish/social/login-sessions/{id}` | 查询原浏览器登录会话 |
| `DELETE /login-sessions/{session_id}` | `DELETE /settings/publish/social/login-sessions/{id}` | 取消原浏览器登录会话 |
| `POST /sau/{platform}/publish` | `POST /tasks/{task_id}/actions/publish` 或 `POST /tasks/{task_id}/actions/publish_all` | **不直接透明代理投稿**。Rust 验证任务授权、模型审稿、媒体/账号状态，写入持久意图；`social-tick` 通过 sidecar 交付一次；超时或故障标记 `unknown`，禁止盲目重发 |

## 隔离验证边界

- `rust/tests/mock_social_sau.py` 的所有账号和投稿均为模拟；模拟服务使用 HMAC 内部鉴权，不提供真实浏览器或平台结果。
- `rust/scripts/smoke-isolated.sh` 运行带独立 PostgreSQL 的 HTTP 集成流程，验证账号、登录/取消、导入、健康检查和拒绝无认证请求；`rust/tests/sau_publish_isolated.sh` 验证三平台意图创建、单次交付、503 不盲重试、原 `publish_jobs` 真回执核对。
- `VIDEOROLL_V2_TEST_SAU_BACKFILL_ONLY=true` 验证历史 `vr2_social_publish_intents` 迁移到原 `operation_inbox`，不更改原 `publish_jobs` 的外部凭据。
- 这里的隔离测试**不代表**真实账号、真实平台、Playwright、Celery 或生产环境已验收。真实平台测试须独立验收发布后回执、审批、未知结果恢复、失败补偿与资源清理。

## 上线约束

必须显式配置可信的 HTTPS/本机 SAU 服务地址、独立服务 HMAC 密钥、相同媒体存储根路径，且只有经审批的真实账号可执行外部投稿。真实投稿不应作为普通回归测试的一部分。旧 Python 生产服务仍运行时，Rust 不得同时对同一任务产生外部提交副作用。
