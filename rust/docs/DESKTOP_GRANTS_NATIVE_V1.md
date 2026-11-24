# Rust 原生 noVNC 桌面临时授权（2026-11-24）

本轮复刻原 Python POST /desktop/grants 与 GET /desktop/authorize，
并保留 Nginx internal auth_request 的独立根路径 GET /desktop/authorize。
这不是普通管理员 Token 认证端点：只有原 trusted-device Cookie 身份
可以创建或使用桌面授权，Rust 的内部 API Token 不得越权发放权限。

安全与持久化边界：
- 延用原 desktop_access_grants 表的字段、唯一 token_hash、scope_json、
  status、expires_at、consumed_at 和 last_error。不另建影子授权表。
- 只持久化 SHA256(token)，以及
  SHA256("videoroll-desktop-session:v1:"+admin_cookie) 身份指纹。
  原始随机令牌只返回一次。令牌由两个独立 UUIDv4 随机值产生，
  约 244 bit 随机熵，URL 安全 Base64 无填充，共 43 字符。
- 初始状态 active，过期 300 秒，WebSocket reconnect_limit=8。
  静态页面、JS/CSS/图片请求只做授权，不消耗重连次数。
  带 /websockify 的真实连接通过 SELECT FOR UPDATE 串行递增；
  第 8 次授权通过并标记 consumed，之后全部拒绝。
- 精确检查 X-Original-URI 的 /social-login/ 或 /social-publish/
  命名空间与 UUID 资源；禁止任意目录访问、百分号编码/路径穿越、
  不同设备 Cookie、不同资源和不同桌面类型混用。
- Nginx 内部代理直接调用根 GET /desktop/authorize 返回 204，
  原后台 /api/v1/desktop/authorize 同样支持内部诊断。
  内部 Token 即使合法也不能替代已签名设备 Cookie。
- 过期令牌由实际授权请求原子标记 expired，非法 scope 拒绝或撤销。
  本轮不部署服务或修改运行中 Nginx 反向代理。

跨 Python/Rust 隔离验证：
VIDEOROLL_V2_TEST_DESKTOP_ONLY=true bash rust/scripts/smoke-isolated.sh

使用临时 PostgreSQL 内 Python V1 desktop_access_grants 原表布局，
由 Python 原版签发/校验 Cookie 与桌面授权，Rust 实际 HTTP 插入、
验证和消费，反向调用 Python 原版 _consume_desktop_grant 消费 Rust
授权。还验证权限界限、到期、资产免扣、8 次连接、第 9 次拒绝、
16 个并发 WebSocket 授权请求的行锁上限。不存在真实 SAU 登录或
生产 noVNC WebSocket 端到端验收，因此仅计部分映射。
