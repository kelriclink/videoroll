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

## 参数（Query）

| 参数 | 必填 | 说明 |
|------|------|------|
| `token` | 是 | Remote API Token |
| `url` | 是 | YouTube 视频链接 |
| `license` | 否 | 授权类型，默认 `authorized` |
| `proof_url` | 否 | 授权证明链接 |
| `auto_publish` | 否 | `true`/`false`，覆盖 Auto Profile 中的投稿设置 |

## 示例

```bash
curl -G "https://your-host/api/remote/auto/youtube" \
  --data-urlencode "token=YOUR_TOKEN" \
  --data-urlencode "url=https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
  --data-urlencode "license=authorized" \
  --data-urlencode "auto_publish=true"
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
- 如果 token 验证失败，返回 401/403 错误

## 注意事项

- 仅提交 YouTube 视频链接
- 不要提交付费墙、私密视频或绕过平台限制的链接
- Token 不要提交到 git
