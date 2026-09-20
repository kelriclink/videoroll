# VideoRoll 安全架构与剩余风险

基线：2026-09-20。

本文说明**当前工程安全边界**。它不是第三方渗透测试报告，也不继续把旧版本已经修复的问题列成“当前 Critical”。

## 1. 管理员认证

当前管理员密码：

- PBKDF2-HMAC-SHA256；
- 200,000 iterations；
- 随机 16-byte salt；
- 长度限制 8..128。

管理员登录后使用：

```text
videoroll_admin_device
```

trusted-device cookie。

Cookie signing key 派生自：

- internal secret；
- 当前管理员 password hash。

因此管理员修改密码后，旧 device cookie 不再通过验证。

当前 device cookie 最大寿命约 180 天，这是易用性取舍。

## 2. 内部服务认证

内部请求使用：

```text
X-Videoroll-Internal-Token
```

并依赖：

- Docker internal network；
- 不公开内部服务 host port；
- shared internal secret。

风险：单个内部容器完全失陷后，共享 secret 的横向影响仍然存在。

长期增强方向：

- per-service identity；
- short-lived signed token；
- mTLS / service mesh。

## 3. 容器基线

应用服务普遍使用：

```text
non-root UID/GID
no-new-privileges
cap_drop: ALL
init: true
```

这降低容器进程权限，但不代表容器逃逸或依赖漏洞不可发生。

## 4. 网络出口

Compose 将外网能力按角色拆分：

- egress；
- subtitle-egress；
- platform-egress；
- playout-egress；
- infrastructure-egress；
- web-ingress。

RAG Agent 不获得通用 shell/socket 工具；网页访问通过注册 tool 和受控 egress 路径。

## 5. Remote API

控制：

- Bearer header only；
- query token 不接受；
- durable Idempotency-Key；
- DB request hash；
- token/IP rate limit；
- per-token concurrent dispatch limit。

Remote API 的 durable 事实保存在 PostgreSQL，而不是 Redis。

详细见 [REMOTE_API.md](REMOTE_API.md)。

## 6. WebSocket

WebSocket 要求：

- 管理员 device cookie；
- Origin 校验；
- topic 校验；
- client message size limit；
- per-connection event queue limit。

Realtime event 本身不提供额外授权能力。

## 7. Desktop / noVNC

`/social-login/` 和 `/social-publish/`：

- 不直接公开 upstream host port；
- 使用短期 DesktopAccessGrant；
- grant 绑定 resource scope；
- Nginx auth subrequest；
- path-scoped HttpOnly grant cookie；
- grant-bearing path 关闭 access log。

外层 proxy 不应绕过 Web 直接公开 6080。

## 8. 平台凭据

Social storage_state / Bilibili credential 等敏感信息：

- 保存时加密；
- Web 不回显明文；
- social worker 仅在 tmpfs 临时解密；
- worker 任务结束清理临时文件。

必须保护：

```text
data/secrets/fernet.key
```

## 9. Security Audit

数据库 `SecurityAuditEvent` 保存：

- event type；
- actor type/id；
- outcome；
- request id；
- source IP；
- bounded payload；
- error code/message。

Audit 不应包含完整 API Key、Cookie 或 Authorization header。

## 10. Trusted Proxy

部署可配置：

- `TRUSTED_PROXY_HOSTS`；
- `TRUSTED_PROXY_CIDRS`。

只有可信外层 proxy 的 Forwarded 信息才应影响 source IP/proto 识别。

错误的 trusted-proxy 配置会影响：

- audit source IP；
- Remote API IP rate limit；
- secure cookie/proto；
- 日志定位。

## 11. 外部网页与 Prompt Injection

RAG Agent 读取的：

- 搜索结果；
- Wikipedia；
-网页正文；

都属于**外部不可信证据**。

它们不能被视为 system/developer instruction。

User Agent Skill 同样在 prompt 中标记为：

```text
untrusted_user_guidance
```

当前主要防线：

- fixed tool registry；
- allowed tool policy；
- input/output schema；
- runtime budget；
- egress control；
- system prompt trust distinction。

## 12. Browser Automation 风险

social publisher 必须运行 Chromium/Patchright。

风险包括：

- 平台页面本身不可信；
- selector / DOM 频繁变化；
- browser dependency 漏洞；
- storage_state 权限较高；
- “超时”不代表“没有发出去”。

因此：

- 浏览器自动化独立容器；
- 推荐专门发布账号；
- `unknown` 不自动重试。

## 13. PostgreSQL

生产数据库在 Compose 外。

部署者负责：

- TLS；
- private network / firewall；
- DB user 最小权限；
- backup；
- restore drill；
- retention。

业务状态不能只备份 `data/` 而忽略 PostgreSQL。

## 14. Redis

Redis 用于：

- Celery broker/backend；
- realtime Pub/Sub；
- rate/concurrency辅助。

不要直接暴露 Redis 到公网。

业务幂等和最终状态不应只依赖 Redis。

## 15. ffplayout

ffplayout 内部端口：

```text
8787
```

默认不映射到宿主机。

浏览器通过 Web 的 `/playout/` 同源入口访问。

额外公开 8787 会绕过 VideoRoll 入口边界。

## 16. Secret 管理

禁止进入 Git：

- `.env`；
- DB password；
- LLM/ASR API Key；
- Remote API Token；
- Bilibili Cookie；
- social storage_state；
- Fernet key；
- 含凭据的生产日志/dump。

怀疑泄漏时：

1. 轮换上游凭据；
2. 更新 VideoRoll setting/env；
3. 重启相关服务；
4. 清理日志/CI artifact；
5. 检查 audit/event 时间窗口。

## 17. 当前剩余风险

### 17.1 无内建 MFA

VideoRoll 自身当前是管理员密码 + trusted device，不含 MFA。

公网部署推荐外层增加：

- VPN；
- SSO；
- Access proxy；
- MFA gateway。

### 17.2 Trusted-device 生命周期较长

180 天适合内网管理，但高安全环境可以考虑：

- 缩短有效期；
- 设备列表；
- 单设备撤销；
- 登录活动页面。

### 17.3 Shared internal secret

当前内部服务身份没有 per-service cryptographic isolation。

### 17.4 第三方平台与浏览器依赖

SAU、Chromium、yt-dlp、平台 API 都是快速变化依赖，需要持续更新和 smoke test。

### 17.5 LLM / Search provider

外部 provider 可能记录请求，部署者需要根据数据敏感性选择供应商和保留策略。

## 18. 安全验证

开发/CI：

```bash
bash ./scripts/security_smoke.sh
docker compose -f docker-compose.yml config --quiet
git diff --check
```

生产：

```bash
./scripts/prod_compose.sh ps
./scripts/prod_compose.sh logs --since 10m
```

同时检查 Web 没有暴露内部服务端口。

## 19. 推荐公网边界

```text
Internet
   │
   ▼
TLS + SSO/VPN/Access/MFA
   │
   ▼
VideoRoll Web
   │
   ▼
internal services
```

不推荐直接把 Docker host 的 8000/8001/8002/8003/8010/8787/6379 暴露公网。
