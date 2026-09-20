# VideoRoll UI 实时事件

VideoRoll Web 使用：

```text
/api/ws/events
```

连接 Orchestrator WebSocket。

PostgreSQL 是最终状态源；Redis Pub/Sub 只传输 best-effort UI change notification。

## 1. 认证与 Origin

WebSocket 使用管理员 trusted-device cookie：

```text
videoroll_admin_device
```

服务端同时检查 Origin：

- 与当前 Host 同源；
- 或在配置的 CORS allow origins 中。

缺失/非法 Origin 不作为正常浏览器连接接受。

## 2. Client Flow

标准页面逻辑：

```text
REST snapshot
   │
   ▼
connect websocket
   │
   ▼
ready
   │
   ▼
set_subscriptions
   │
   ▼
merge incremental events
```

断线或收到：

```json
{"type":"resync_required"}
```

时：

1. 重新加载 REST snapshot；
2. 重新建立订阅。

## 3. Subscription

客户端消息：

```json
{
  "op": "set_subscriptions",
  "topics": ["tasks", "queue"]
}
```

固定 topics：

- `tasks`；
- `queue`；
- `resources`；
- `agents`；
- `publishing`。

Task topic：

```text
task:<uuid>
```

单连接最多订阅：

```text
32 topics
```

## 4. 消息与队列限制

客户端单条消息最大：

```text
8 KiB
```

服务端每个连接 queue：

```text
256 events
```

queue overflow 时不会无限堆积，而是排入：

```json
{
  "type": "resync_required",
  "reason": "client_queue_overflow"
}
```

## 5. Heartbeat

服务端周期发送 heartbeat。

浏览器回复：

```json
{"op":"pong"}
```

断线后前端按有界指数退避重连。

## 6. 主要事件

当前包括：

- `task.updated` / `task.deleted`；
- `subtitle_job.updated` / `subtitle_job.deleted`；
- `render_job.updated` / `render_job.deleted`；
- `publish_job.updated` / `publish_job.deleted`；
- `publish_batch.updated` / `publish_batch.deleted`；
- `asset.updated` / `asset.deleted`；
- `task_queue.changed`；
- `publish_account.updated` / `publish_account.deleted`；
- `login_session.updated`；
- `agent_run.started`；
- `agent_run.step_appended`；
- `agent_run.finished`；
- `system.resources.sample`；
- `log.updated`。

具体 payload 可以随业务实体演进；前端应只依赖自己使用的字段并保留 REST resync。

## 7. Logs

日志正文**不通过 WebSocket**传输。

当 Logs tab 打开时，`log.updated` 只表示日志发生变化；前端再通过 storage/API 做 ranged read。

这样可以避免：

- 大日志占 Redis；
- 大日志占 WebSocket queue；
- 慢客户端拖累实时通道。

## 8. Delivery Semantics

Realtime 不是 durable event stream。

允许：

- Redis Pub/Sub event 丢失；
- Redis 重启；
- browser queue overflow；
- browser 离线。

正确性依赖：

```text
REST snapshot + PostgreSQL
```

而不是“必须收到每一个 WebSocket event”。

## 9. Nginx

`src/web/nginx.conf` 的 `/api/ws/` 当前配置：

- HTTP/1.1；
- Upgrade / Connection；
- 保留浏览器 Host；
- 长 read/send timeout；
- proxy buffering off；
- access log off。

外层反向代理也必须支持 WebSocket upgrade。
