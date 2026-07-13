# 远程自动接入 API

通过 Remote API 可以从外部脚本或 Agent 向 VideoRoll 推送 YouTube 视频，自动进入处理流水线。

## 配置

在 VideoRoll Web UI 的 `Settings → API` 页面中：

1. 生成或修改 Remote API Token
2. 记下 Remote API 端点地址

## 端点

```
POST /api/remote/auto/youtube
```

仅接受 `application/json` 请求体。Token 只能放在 `Authorization` 请求头，绝不能放在 URL、查询字符串或日志中。

## 请求头

| 参数 | 必填 | 说明 |
|------|------|------|
| `Authorization` | 是 | `Bearer <Remote API Token>` |
| `Idempotency-Key` | 是 | 单个逻辑请求的稳定唯一键；网络重试时必须复用该键 |
| `Content-Type` | 是 | `application/json` |

## JSON 请求体

| 字段 | 必填 | 说明 |
|------|------|------|
| `url` | 是 | YouTube 视频链接 |
| `license` | 否 | 授权类型，默认 `authorized` |
| `proof_url` | 否 | 授权证明链接 |
| `auto_publish` | 否 | 布尔值，覆盖 Auto Profile 中的投稿设置 |

## 示例

```bash
curl -X POST "https://your-host/api/remote/auto/youtube" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Idempotency-Key: 1f9aa7de-remote-request-001" \
  -H "Content-Type: application/json" \
  --data '{
    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "license": "authorized",
    "auto_publish": true
  }'
```

## 响应

```json
{
  "task_id": "...",
  "pipeline_job_id": "...",
  "deduped": false,
  "source_id": "..."
}
```

- `deduped=true` 表示该 URL 已存在，不会重复创建任务
- `deduped=true` 时不会重复派发自动流水线；如需重新处理，请使用已登录管理员的任务重启操作
- 相同 Token 与 `Idempotency-Key` 再次提交相同 JSON，将直接返回第一次的结果，不会重复派发
- 相同 Token 与 `Idempotency-Key` 但不同 JSON 返回 `409 Conflict`
- 如果 Bearer token 验证失败，返回 `401`；未配置 token 时返回 `403`

## 迁移旧调用

旧的 `GET /api/remote/auto/youtube?token=...&url=...` 合约已移除，当前固定返回 `410 Gone`。即使把 `token` 放到新的 `POST` 查询字符串中也不会被读取，结果是 `401`。请迁移为本页的 Bearer `POST` JSON 合约。

迁移顺序：

1. 先在调用端支持 JSON 请求体和 `Idempotency-Key`，但保留原调用的观测。
2. 将 token 从配置、URL 模板、代理参数和日志字段中删除，只在进程内构造 `Authorization` header。
3. 用同一个逻辑请求重复调用一次，确认返回同一结果且未重复创建流水线。
4. 删除旧 GET 调用；不要把 `410` 当作可重试错误。

幂等记录保留 24 小时；在该时间窗内重试同一逻辑请求时必须复用原 `Idempotency-Key`。超过窗口后，调用方必须使用新的键并自行决定是否仍应提交。

## 日志与泄露处理

- 不要把 token 放进 URL、query、shell 历史、截图、任务描述或异常文本。
- 反向代理和调用方日志只记录请求 ID、HTTP 状态和 token 指纹，不能记录 `Authorization` 值。
- 怀疑 token 出现在 URL 或日志中时，立即在 `Settings → API` 轮换 token，清理相关日志副本，并将旧 token 视为失效。

Remote API 没有兼容开关；回滚部署也不得恢复 query-token 或 GET 写入语义。

## 注意事项

- 仅提交 YouTube 视频链接
- 不要提交付费墙、私密视频或绕过平台限制的链接
- Token 不要提交到 git、URL 或查询字符串
- 不要为不同请求重用 `Idempotency-Key`
