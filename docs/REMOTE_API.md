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

旧的 `GET /api/remote/auto/youtube?token=...&url=...` 合约已废弃，当前返回 `410 Gone`。请迁移为本页的 Bearer `POST` JSON 合约。幂等记录会保留 24 小时；在该时间窗内重试同一逻辑请求时必须复用原 `Idempotency-Key`。

## 注意事项

- 仅提交 YouTube 视频链接
- 不要提交付费墙、私密视频或绕过平台限制的链接
- Token 不要提交到 git、URL 或查询字符串
- 不要为不同请求重用 `Idempotency-Key`
