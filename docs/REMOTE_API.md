# VideoRoll Remote API

Remote API 用于从外部脚本、浏览器扩展或可信 Agent 提交 YouTube URL，并进入 VideoRoll 自动流水线。

当前入口：

```http
POST /api/remote/auto/youtube
```

旧的 GET/query-token 合约已经移除。

## 1. 配置

在 Web：

```text
Settings → API
```

设置 Remote API Token。

Token 保存后不会回显明文。

## 2. Headers

| Header | 必填 | 说明 |
|---|---:|---|
| `Authorization` | 是 | `Bearer <token>` |
| `Idempotency-Key` | 是 | 单个逻辑请求的稳定唯一键，最大 255 字符 |
| `Content-Type` | 是 | `application/json` |

Token **不能**放在 URL 或 query string。

## 3. Request Body

```json
{
  "url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "license": "authorized",
  "proof_url": "https://example.com/proof",
  "auto_publish": false
}
```

字段：

| 字段 | 必填 | 说明 |
|---|---:|---|
| `url` | 是 | YouTube 视频 URL |
| `license` | 否 | `own` / `authorized` / `cc` / `unknown` |
| `proof_url` | 否 | 授权证明 URL |
| `auto_publish` | 否 | 覆盖 Auto Profile 的自动投稿开关 |

## 4. 示例

```bash
curl -X POST 'https://videoroll.example/api/remote/auto/youtube' \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -H 'Idempotency-Key: ingest-20260920-0001' \
  -H 'Content-Type: application/json' \
  --data '{
    "url": "https://www.youtube.com/watch?v=VIDEO_ID",
    "license": "authorized",
    "auto_publish": false
  }'
```

## 5. Response

典型响应：

```json
{
  "task_id": "...",
  "pipeline_job_id": "...",
  "deduped": false,
  "source_id": "..."
}
```

`deduped=true` 表示 source 已存在，不会重复创建同一接入任务或重复派发自动流水线。

## 6. 幂等语义

VideoRoll 使用：

```text
SHA-256(token) + Idempotency-Key
```

唯一约束 Remote API 请求。

同一个 key：

- 相同 payload：返回第一次持久化结果；
- 不同 payload：`409 Conflict`；
- 之前记录为 failed：`409`，调用方需要换新 key；
- 已 reservation 但尚未完成：`409`，表示请求正在执行。

记录窗口：

```text
24 hours
```

数据库是幂等事实源。Redis 重启不会导致已 reservation 的逻辑请求自动重复派发。

为减少敏感信息持久化，Remote API 幂等表保存 request hash，而不是完整 source/proof URL payload。

## 7. 限流与并发

当前代码常量：

| 维度 | 限制 |
|---|---:|
| 每 token | 60 requests / 60s |
| 每 source IP | 120 requests / 60s |
| 每 token 同时 dispatch | 4 |

Redis 用于这些 rate/concurrency guard。

如果 Redis guard 临时不可用，系统会记录 warning 并继续依赖数据库的 durable idempotency；因此 Redis 不决定“是否已经处理过这个逻辑请求”。

## 8. HTTP 状态

| 状态 | 含义 |
|---|---|
| `2xx` | 新请求成功或幂等 replay |
| `400` | 缺 Idempotency-Key / 参数错误 |
| `401` | 缺 Bearer 或 Token 无效 |
| `403` | 服务器尚未配置 Remote API Token |
| `409` | key/payload 冲突、请求正在执行、旧请求明确失败 |
| `429` | rate/concurrency limit |
| `503` | durable reservation/result 无法持久化 |
| `410` | 旧 GET 合约已移除 |

遇到 `429` 时调用方应遵守 `Retry-After`。

## 9. Browser Extension

仓库提供：

```text
extensions/videoroll-youtube-submit/
```

构建：

```bash
./scripts/build_browser_extension.sh
```

输出：

```text
dist/videoroll-youtube-submit.zip
```

扩展会把 endpoint/token 保存在浏览器扩展本地存储，并为“请求结果不确定”的网络失败保留原 Idempotency-Key，避免重复创建流水线。

## 10. 调用方最佳实践

- 一个逻辑请求只生成一个 Idempotency-Key；
- 网络重试必须复用原 key；
- payload 改变时使用新 key；
- `409 previously failed` 不要无限重试；
- `429` 尊重 Retry-After；
- 不要把 Token 放 URL、日志、任务名称或截图。

## 11. 迁移旧调用

以下方式已废弃：

```text
GET /api/remote/auto/youtube?token=...
```

请改为：

- POST；
- Bearer header；
- JSON body；
- Idempotency-Key。

不要把 `410 Gone` 当成瞬时错误重试。
