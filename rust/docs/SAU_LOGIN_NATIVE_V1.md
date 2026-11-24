# Rust 原生 SAU 浏览器登录会话

原版三条独立 API 已接入 Rust：

- POST /login-sessions/{platform}：原 account_name JSON，返回原 SocialLoginSessionRead。
- GET /login-sessions/{session_id}：查询会话，未知 UUID 返回 404。
- DELETE /login-sessions/{session_id}：取消待执行或运行中的会话，已终态会话原样返回。

会话 ID、状态、消息及时间持久化在原 operation_inbox，独立 runtime_engine=rust-sau-login，不新增业务表。全局活动会话局部唯一索引与事务锁保护共享桌面；每个平台最新会话索引用于原 publishing WebSocket topic 的 login_session.updated 事件，重连读取最新状态。会话记录不存储 Cookie。原 accounts 主键与凭据格式不变，登录成功后事务性保存账号及终态；已有账号保留 ID，使用账号状态及凭据轮换时间核对，取消、失败及检测到账号变化均不得覆盖原凭据。

配置要求：
- 先应用 Rust migrations。显式设置 VIDEOROLL_V2_SAU_ENABLE_NATIVE_CHECK=true 与 VIDEOROLL_V2_SAU_ENABLE_NATIVE_LOGIN=true，共享一个原生 SAU 执行器，登录与检查串行。旧 Python 登录/导入/检测 Worker 必须停止并排空，不得并行管理同一运行时。
- SAU_EXECUTABLE、SAU_RUNTIME_DIR、SAU_COOKIES_DIR 仍使用第三方 SAU CLI，启动参数为 platform login --account name --headed；复用原 DISPLAY、DOUYIN_COOKIE_AUTH_HEADLESS、SOCIAL_LOGIN_TIMEOUT_SECONDS（默认900秒）配置。登录 URL 默认为原 /social-login/vnc.html 路径，只接受该同源路径前缀。
- 必须保留并配置独立 X11/noVNC/websockify 及受保护的 VNC bridge。Rust API 并不实现 RFB 桌面服务，不开放无认证 VNC。
- 原生 Worker 限制输出文件权限、不返回 CLI 原始日志，拒绝覆盖残留 Cookie。过期运行租约恢复为 failed 或 canceled，禁止自动重放浏览器；强制终止可能遗留文件，需确认旧浏览器子进程退出后安全清理。真实浏览器子进程、VNC 和平台认证仍待验收。
- 原账号检测及投稿入口在同一账号登录期间拒绝修改或提交。当前投稿执行仍依赖原 SAU Worker / Outbox Dispatcher，不能因 API 矩阵完成而直接停止所有 Python 组件。

隔离测试（完整迁移、一次性 PostgreSQL、模拟 CLI、不实际登录平台）：
VIDEOROLL_V2_TEST_SAU_NATIVE_LOGIN_ONLY=true bash rust/scripts/smoke-isolated.sh

该测试覆盖原响应结构、成功保存、互斥、取消、超时、凭据冲突、重启恢复，以及原生 WebSocket 推送和重连快照。
