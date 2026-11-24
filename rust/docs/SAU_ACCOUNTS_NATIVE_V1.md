# Rust 独立 SAU 账号服务的增量迁移（2026-11-24）

本轮在独立 Rust binary videoroll-sau 上完成原 Python social_publisher
的三条接口，**不在 Orchestrator /api/v1 上伪造另一组路由**：

- GET /health（不需 Token），返回 {"status":"ok","driver":"sau"}
- GET /accounts 和 ?platform=douyin|xiaohongshu|kuaishou
- DELETE /accounts/{account_id}

每条部分映射均可在真正启动的 Rust 独立 HTTP 服务中执行，不需要
Python SAU API 转发。保持 original accounts 和 publish_jobs 表结构，
不添加 vr2_* 影子表，不解密、不返回、不记录原 secrets_encrypted。

运行边界：
- 独立进程使用 DATABASE_URL、INTERNAL_API_SECRET、
  VIDEOROLL_V2_SAU_BIND（默认 127.0.0.1:18087）。
- 首选 Docker 专属内部网络或回环；非回环地址必须显式
  VIDEOROLL_V2_SAU_ALLOW_CONTAINER_BIND=true，并由防火墙禁止外部访问。
- /accounts 系列依照原 Python service_token 协议校验
  X-Videoroll-Internal-Token，即 v1. 加 HMAC-SHA256
  (INTERNAL_API_SECRET, "videoroll-internal-service:v1")。
  无凭据返回 401，错误凭据返回 403；不使用通用 Rust 管理 Token。
- 账号列表按 platform/name 排序，过滤三种社交平台，输出原
  SocialAccountRead 的全部八个非敏感字段。
- DELETE 使用原 accounts 行 SELECT FOR UPDATE，与 Python 创建投稿
  时的行锁兼容；遇到 checking 或该账号任何 submitting 投稿即 409。
  成功则原地 is_active=false、清空 secrets、check_state=unchecked，
  清除 last_check_message，但不改变历史 rotated_at/last_checked_at，
  不删除或修正原 publish_jobs，尤其不能消除 unknown 收据。
- 已撤销账号重复请求仍返回 {"ok":true}，缺失 ID 与 B 站账号 404。

安全和未完成的功能：
- 此 binary **没有**实现 /accounts/{platform} 的 Cookie 导入、
  /accounts/{account_id}/check 的 SAU CLI 校验、
  /login-sessions/* 的浏览器会话或 /sau/{platform}/publish 的
  持久化投稿。对应 6 条旧版接口继续保持未映射状态。
- 原 Python BrowserLoginManager._sessions 是进程内状态，不能
  伪装成已经被 Rust 接管。实际 SAU 平台自动化和登录不应因本轮
  直接将生产 SOCIAL_PUBLISHER_URL 指向只提供 3 条接口的 Rust binary。
- 不能因账本返回 unknown 而盲目重试；Rust orchestrator 的原生
  SAU outbox/receipt 需要独立故障注入和真实平台验证。

专项验证：

VIDEOROLL_V2_TEST_SAU_ACCOUNTS_ONLY=true bash rust/scripts/smoke-isolated.sh

该脚本启动临时 PostgreSQL、独立 Rust SAU binary 和 Python 原版 ORM，
比较原 account_read 输出、三个平台筛选、HMAC 权限、账号忙保护、
跨平台隔离、无敏感 Cookie 回显、四路并发幂等撤销和重启后状态；
模拟 publish_jobs.state=unknown 时保留原外部回执。
完整隔离回归也会执行该子套件。**均不写入真实生产数据库。**

此处属于“接口部分映射”而非完整生产等价签收。
