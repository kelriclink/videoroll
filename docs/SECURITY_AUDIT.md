# VideoRoll 项目审计报告

## 2026-07 安全架构上线状态

本节覆盖本轮上线已落实的边界，并替代下文与其矛盾的历史风险描述。历史条目仍保留，供追踪尚未处理的风险。

| 边界 | 当前状态 | 验证 |
|---|---|---|
| 内部服务 | 所有非 `/health` 请求需要 `X-Videoroll-Internal-Token`，服务仅在 internal Compose 网络 | `scripts/security_smoke.sh` |
| Remote API | 只接受 Bearer `POST` JSON 与 `Idempotency-Key`；旧 GET/query-token 为 `410` | `tests/test_security_rollout.py` |
| 异步副作用 | domain 事务与 outbox 事件同事务提交，broker 失败保留为可重试状态 | `tests/test_security_rollout.py` |
| noVNC | 短期、会话/资源绑定的 desktop grant 保护 landing page 与 WebSocket | `tests/test_security_rollout.py` |
| 外部抓取 | egress gateway 复核 DNS、redirect 和连接 peer，拒绝私网/非全局地址 | `tests/test_security_rollout.py` |

### 上线前检查

```bash
./scripts/security_smoke.sh
```

该脚本不启动 Compose，也不访问外网；它验证内部认证、内部服务未发布宿主机端口、URL 凭证拒绝、desktop grant 作用域、outbox 重试和私网 egress 拒绝。完整回归仍按 CI 流程执行。

### 回滚限制

可以停止入口流量、回退应用镜像、修复 Redis/worker 并让 outbox 重试，或暂停 interactive desktop 的业务入口。不得通过任何环境变量、nginx 临时规则或旧镜像恢复以下能力：

- Remote API 的 GET 或 query-token；
- 未认证 noVNC 或公开 VNC/websockify 端口；
- 内部 API 的宿主机端口映射或无内部 service token 调用；
- 绕过 egress gateway 的私网/任意目标抓取。

Schema 增量应保留以维护历史任务、outbox 和授权记录；数据库降级需要单独的备份恢复计划，不能作为事故中的即时回退动作。

## 需要立即修复 (Critical)

### 1. Fernet 密钥管理缺失
- **文件**: `src/videoroll/utils/fernet.py`
- 密钥文件不存在时自动生成新密钥，无备份/轮转机制
- 密钥丢失 = 所有加密的 YouTube cookies、Bilibili tokens、OpenAI API keys 永久不可恢复
- 无密钥轮转支持，`@lru_cache` 使密钥在进程生命周期内无法刷新
- **改进**: 启动时检查密钥是否存在，不存在则报错退出而非静默生成；支持密钥轮转

### 2. 登录接口无防暴力破解
- **文件**: `src/videoroll/apps/orchestrator_api/main.py` (`/auth/login`)
- 无速率限制，可无限次尝试密码
- PBKDF2 200k 迭代提供了一定的单次计算成本，但不足以作为唯一防线
- **改进**: 加入 `slowapi` 或类似限流库，限制同一 IP 的登录尝试频率

### 3. Remote API token 通过 query 参数传递（已修复）
- **文件**: `src/videoroll/apps/orchestrator_api/remote_api_settings_store.py`
- `?token=` 出现在 access log、代理日志、浏览器历史、Referer header 中
- token 虽然在存储时做了 PBKDF2 hash，但传输过程中明文暴露
- **现状**: 仅接受 `Authorization: Bearer` 的 JSON `POST`；旧 GET/query 合约返回 `410 Gone`

---

## 高优先级 (High)

### 4. orchestrator_api/main.py 是 God File
- **文件**: `src/videoroll/apps/orchestrator_api/main.py` (2900+ 行)
- 80+ 路由、CORS 配置、认证中间件、后台线程（清理、YouTube 扫描）全部在一个文件中
- 错误处理不统一：有的直接 `raise HTTPException`，有的 `try/except httpx.HTTPError`，有的 `except Exception: pass`
- `_ingest_youtube_source` 在多处被调用，但内部自行创建 `httpx.Client` 和 `SessionLocal`，无法参与外部事务
- **改进**: 按职责拆分为独立 router 模块（auth、tasks、settings、youtube、maintenance）；统一错误处理中间件

### 5. Celery worker 无重试和确认机制
- **文件**: `src/videoroll/apps/subtitle_service/worker.py`
- `process_job`、`process_render_job`、`task_queue_tick` 均未使用 `autoretry_for`、`retry()`、`acks_late`
- worker 被 OOM/SIGKILL 杀掉后任务直接丢失，仅有的恢复逻辑 `_recover_interrupted_subtitle_jobs()` 将任务标记为 FAILED 而非重试
- 翻译重试使用 `while True` + `time.sleep()` 阻塞 worker 线程
- **改进**: 加 `acks_late=True` + `autoretry_for` 或手动 `retry()`；翻译重试改用 Celery 内置 retry 机制

### 6. 容器以 root 运行
- **文件**: `Dockerfile`
- 无 `USER` 指令，容器进程以 root 身份运行
- 应用被攻破后攻击者拥有完整 root 权限
- **改进**: 加 `RUN useradd -r -s /usr/sbin/nologin videoroll` 和 `USER videoroll`

### 7. 内部服务间认证 token 从 S3 secret 派生（已修复）
- **文件**: `src/videoroll/utils/internal_api_token.py`
- token = `SHA256("videoroll-internal-token:v1:" + s3_secret_access_key)`
- S3 secret 轮转后所有内部认证同时失效，无独立的内部密钥配置
- 无 HMAC、无 per-service nonce、无过期机制
- **现状**: 独立的 `INTERNAL_API_SECRET` 经版本化 HMAC 派生，生产环境拒绝空值和已知默认值

### 8. 安全相关 cookie 配置问题
- **文件**: `src/videoroll/apps/orchestrator_api/main.py`
- 设备 cookie 有效期 180 天，无会话撤销机制，被盗 cookie 可用半年
- 非 HTTPS 时不设 `Secure` 标志，cookie 以明文传输
- `allow_methods=["*"]` + `allow_credentials=True` 配置过于宽松
- 密码 hash 缓存在 app.state 中，改密码后旧 hash 不会立即失效
- **改进**: 缩短 cookie 有效期；始终设 `Secure`；限制 CORS methods/headers；密码变更时清理缓存

---

## 中优先级 (Medium)

### 9. Auto-migration 功能受限
- **文件**: `src/videoroll/db/auto_migrate.py`
- 只能 `ALTER TABLE ADD COLUMN`，无法处理：重命名列、改类型、建新表、加约束/索引、删列、数据迁移
- `@lru_cache` 使 rolling deploy 时旧进程不会执行新 migration
- `_add_column` 使用字符串拼接构造 SQL，虽然当前是硬编码值但存在注入隐患
- **改进**: 如果 schema 变更频繁，考虑引入 Alembic；至少将 SQL 构造改为参数化

### 10. 代码重复
- `_effective_youtube_settings` 在 `orchestrator_api/main.py` 和 `subtitle_service/worker.py` 中重复
- `_read_s3_bytes` 在两个文件中几乎完全相同
- `_safe_append_log_line`、`_safe_append_log_block`、`_cleanup_local_work_root` 等辅助函数仅存在于 worker.py 但其他模块也需要
- **改进**: 提取到 `videoroll/utils/` 共享模块

### 11. Docker entrypoint 单点故障
- **文件**: `docker/entrypoint.sh`
- 三个进程并行运行（uvicorn + 2 celery workers），一个挂了整个容器退出，无重启逻辑
- 没有 `exec` 启动 uvicorn，SIGTERM 发给 bash 而非 uvicorn，信号处理可能异常
- 无健康检查 / readiness probe
- **改进**: 考虑用 supervisord 或拆分为独立容器；对 uvicorn 使用 `exec`

### 12. 前端类型安全弱
- **文件**: `src/web/src/lib/types.ts`, `src/web/src/lib/http.ts`
- `Asset.kind`、`SubtitleJob.status`、`PublishJob.state` 用 `string` 而非联合类型
- `fetchJson` 做 `as T` 强转，无运行时校验，API 响应变化时静默产生错误数据
- 部分页面用 `alert()`/`confirm()` 做用户反馈
- **改进**: 将已知枚举定义为 union types；关键 API 响应加 zod 运行时校验

### 13. 依赖版本只有下限没有上限
- **文件**: `pyproject.toml`
- `cryptography>=42.0.0`、`fastapi>=0.115.0`、`yt-dlp>=2026.2.4` 等均无上界
- 未来 `pip install` 可能拉入不兼容或有安全问题的版本
- **改进**: 对安全敏感的包（cryptography、sqlalchemy、psycopg）加 `<next_major` 上界

### 14. 大量错误被静默吞掉
- 散布在 `orchestrator_api/main.py` 和 `subtitle_service/worker.py` 中的 `except Exception: pass`
- cookie 文件处理、S3 操作等关键路径中的异常被忽略，生产环境排查困难
- **改进**: 至少加 `logger.warning()` 记录被吞掉的异常；关键路径不吞异常

### 15. S3_USE_SSL 默认关闭
- **文件**: `src/videoroll/config.py`
- `s3_use_ssl: bool = Field(False, ...)` 导致默认情况下数据在网络上明文传输
- **改进**: 生产环境默认 `True`，仅在开发配置中显式关闭

---

## 低优先级 (Low)

### 16. 测试覆盖不足
- 纯函数（`publish_meta_rules`、`publish_review`）测试质量好
- 缺少：API 层集成测试、Celery 任务测试、auth 中间件测试、错误路径测试
- `test_translate_resume.py` 手动构造假 `httpx` 模块，脆弱易碎
- **改进**: 补充 API endpoint 测试（用 `TestClient`）；增加 Celery 任务的单元测试

### 17. `lru_cache` 导致测试间状态泄漏
- `get_orchestrator_settings()` 等被 `@lru_cache` 住，测试间无法重置配置
- **改进**: 提供 `clear_settings_cache()` 辅助函数，或在测试中使用 `@pytest.fixture` 管理 cache

### 18. 硬编码超时和魔法数字
- `_TASK_QUEUE_LOCK_TTL = 300`、`_TASK_QUEUE_HEARTBEAT_INTERVAL_SECONDS = 30` 等散布在代码中
- 环境变量解析 `int(value)` 无校验，非法值会在导入时崩溃
- **改进**: 将可配置值移入 pydantic Settings；加 `Field(gt=0)` 校验

### 19. Dockerfile 安装了不必要的系统包
- `pciutils`、`clinfo`、Intel GPU 驱动无条件安装，增加镜像体积和攻击面
- **改进**: 将 Intel GPU 相关包改为可选的 build arg 控制

### 20. 密码复杂度要求过低
- **文件**: `src/videoroll/apps/orchestrator_api/admin_auth_store.py`
- `validate_new_password()` 仅检查长度 8-128 字符，无复杂度要求
- **改进**: 加入基本复杂度校验（大小写+数字+特殊字符）或常见密码检查
