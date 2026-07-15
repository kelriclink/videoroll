# UI realtime events

The authenticated Web UI uses one WebSocket connection per browser at
`/api/ws/events` (`/ws/events` on the orchestrator). PostgreSQL remains the
authoritative state store; Redis Pub/Sub only carries best-effort change
notifications on `videoroll:ui-events:v1`.

## Client flow

1. Load the page snapshot through the existing REST endpoints.
2. Connect with the normal `videoroll_admin_device` cookie.
3. Send `{"op":"set_subscriptions","topics":[...]}` after the `ready` message.
4. Merge event payloads into local state. On reconnect or
   `resync_required`, load one new REST snapshot and resubscribe.

Supported topics are `tasks`, `queue`, `resources`, `agents`, `publishing`,
and `task:<uuid>`. A connection may subscribe to at most 32 topics.

The server sends a heartbeat every 20 seconds. The browser replies with
`{"op":"pong"}` and reconnects with bounded exponential backoff when the
connection becomes stale.

## Events

- `task.updated` / `task.deleted`
- `subtitle_job.updated` / `subtitle_job.deleted`
- `render_job.updated` / `render_job.deleted`
- `publish_job.updated` / `publish_job.deleted`
- `publish_batch.updated` / `publish_batch.deleted`
- `asset.updated` / `asset.deleted`
- `task_queue.changed`
- `publish_account.updated` / `publish_account.deleted`
- `login_session.updated`
- `agent_run.started` / `agent_run.step_appended` / `agent_run.finished`
- `system.resources.sample`
- `log.updated`

Logs are deliberately not transported over WebSocket. When the task Logs tab
is open, `log.updated` triggers a debounced S3 Range tail request, limited to
one request every two seconds.

## Delivery and deployment

Database commits do not depend on Redis or WebSocket availability. If an
event is lost or a per-client queue overflows, the client restores state from
REST. Nginx must pass WebSocket upgrade headers for `/api/ws/`; Vite development
proxying must keep `ws: true` enabled.
