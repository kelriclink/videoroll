# VideoRoll 社交平台投稿

VideoRoll 将浏览器自动化投稿隔离在独立镜像：

```text
videoroll-social-publisher:prod
```

运行三个服务：

- `social-publisher-api`；
- `social-publisher-worker`；
- `social-publisher-scheduler`。

当前主要支持：

- 抖音；
- 小红书；
- 快手。

Bilibili 使用独立 publisher，不走 social-auto-upload。

## 1. 依赖

运行时基于 Git submodule：

```text
social-auto-upload
```

初始化：

```bash
git submodule update --init --recursive
```

social-publisher 镜像会安装：

- SAU；
- Chromium/Patchright；
- Xvfb；
- x11vnc / noVNC。

## 2. 为什么隔离

浏览器自动化带来更大的依赖和攻击面，因此不进入：

- Orchestrator；
- subtitle worker；
- Bilibili publisher。

主应用只通过内部 API、数据库和 Celery queue 传递任务。

## 3. 常用运行参数

```dotenv
SOCIAL_PUBLISHER_URL=http://social-publisher-api:8010
SOCIAL_PUBLISH_CONCURRENCY=1

SAU_HEADLESS=true
DOUYIN_COOKIE_AUTH_HEADLESS=true

SAU_ACCOUNT_CHECK_TIMEOUT_SECONDS=120
SAU_UPLOAD_TIMEOUT_SECONDS=3600
SAU_LOCK_MARGIN_SECONDS=600

SOCIAL_LOGIN_TIMEOUT_SECONDS=900
```

实际默认值以 `.env.example` 和 Compose 为准。

## 4. 网页登录

Settings → 投稿可以创建 login session。

流程：

1. API service 启动临时 Xvfb desktop；
2. 启动 headed Chromium；
3. 用户通过 `/social-login/` noVNC 操作；
4. 完成扫码、短信或安全校验；
5. 读取 Playwright/Patchright `storage_state`；
6. 使用 Fernet 加密并保存；
7. 删除临时明文文件。

## 5. Desktop Grant

noVNC 不是公开裸桌面。

浏览器登录 URL 使用短期 Desktop Grant。

Web Nginx 通过：

```text
/internal/desktop-auth
```

向 Orchestrator 做 auth subrequest，校验：

- administrator trusted-device session；
- grant；
- resource scope。

landing page 会把 grant 写入 path-scoped HttpOnly Cookie，让 noVNC 的静态资源和 WebSocket 继续接受同一授权检查。

包含 grant 的桌面路径关闭 access log，避免短期凭据进入普通访问日志。

## 6. 本地生成 Storage State

也可以直接使用 SAU：

```bash
cd social-auto-upload
uv pip install -e .
patchright install chromium

sau douyin login --account creator
sau xiaohongshu login --account creator
sau kuaishou login --account creator
```

得到类似：

```text
cookies/douyin_creator.json
cookies/xiaohongshu_creator.json
cookies/kuaishou_creator.json
```

这些是完整 `storage_state`，可能同时包含：

- cookies；
- origins；
- localStorage。

普通 Cookie 字符串不能可靠替代它们。

## 7. 手动导入

Web 上传 storage_state JSON 后：

1. 校验格式和大小；
2. 使用 Fernet 加密；
3. 写数据库；
4. 派发 account check；
5. Worker 在 tmpfs 中临时解密；
6. 命令结束清理明文。

账号 JSON 不保存到共享媒体 `/storage`。

## 8. Account 状态

常见状态：

- `queued`；
- `checking`；
- `valid`；
- `invalid`；
- `error`。

只有平台启用且账号状态可用时，Orchestrator 才应接受新的真实投稿。

## 9. 发布流程

```text
Task / video_final
      │
      ▼
PublishBatch
      │
      ▼
PublishJob
      │
      ▼
social_publish Celery queue
      │
      ▼
SAU / browser automation
```

Worker 从共享 storage 读取视频和封面。

需要 headed browser 的任务可以通过：

```text
/social-publish/
```

观察 worker desktop。

## 10. Publish 状态

当前统一发布状态包括：

- `draft`；
- `submitting`；
- `submitted`；
- `published`；
- `unknown`；
- `failed`。

### submitted

自动化流程正常结束，但系统不一定已经拿到平台内容 ID。

### unknown

浏览器已经开始外部副作用，但超时/异常使结果无法确认。

**unknown 不自动重试。**

再次提交前应先到平台创作者后台确认，避免重复发布。

### failed

表示系统拿到了较明确的失败结果，但仍应根据 error 信息判断是否适合人工重试。

## 11. Scheduler

`social-publisher-scheduler` 使用 Celery beat。

API / worker / scheduler 应部署**相同应用版本**，因为它们共享数据库 schema 和 migration code。

错误部署示例：

```text
database head = 0006_agent_runtime
social image only knows migration 0005
→ startup migration cannot locate revision
```

因此生产升级不能长期只更新 Orchestrator 而不更新 social publisher 镜像。

## 12. Secret

必须保护：

```text
data/secrets/fernet.key
```

替换或删除后，数据库中已有加密账号状态可能无法恢复。

## 13. 网络

social-publisher 服务默认不发布宿主机 API/noVNC 端口。

网络：

- internal；
- platform-egress（API/worker）。

Web 只通过内部 Docker DNS 反代 noVNC。

## 14. 安全建议

- 使用专门投稿账号；
- 新平台/新 SAU 版本首次投稿人工监督；
- 不公开 6080/noVNC；
- 不把 storage_state 提交 Git；
- `unknown` 后不要自动重试；
- 平台页面结构变化时优先检查 SAU submodule 和 selector。
