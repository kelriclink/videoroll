# PublishService 统一投稿编排层设计

> Date: 2026-07-12
> Status: Approved

## 背景

当前自动模式（auto_youtube_pipeline）硬编码只投 B站，完全不读取「投稿设置」里用户已勾选的平台。手动模式和自动模式的投稿逻辑散落在 `subtitle_service/worker.py` 和 `orchestrator_api/services/publishing_service.py` 中，缺乏统一编排。

## 目标

- 创建 `PublishService` 作为统一投稿入口（B3 方案：独立共享模块）
- 自动模式读取投稿设置，对所有已启用平台逐个投稿
- 手动模式保持单平台精确控制
- 每个平台独立错误隔离，一个平台失败不影响其他

## 架构

```
自动模式: subtitle_service worker → PublishService.publish(task_id) → 遍历已启用平台
手动模式: orchestrator API        → PublishService.publish_one(task_id, platform) → 单平台
                                      ↓
                              bilibili_publisher API / social_publisher API
```

PublishService 是纯 Python 模块（`src/videoroll/apps/publish_service.py`），不独立运行，被调用方主动调用。

## 核心设计

### PublishService 类

```python
class PublishService:
    def __init__(self, db: Session, settings: OrchestratorSettings, s3: S3Store): ...

    def publish(self, task_id: UUID, *, publish_payload: dict | None = None) -> PublishAllResult:
        """自动模式：读取投稿设置里所有已启用平台，逐个投稿。"""

    def publish_one(self, task_id: UUID, platform: str, *,
                    payload: PublishActionRequest) -> RemotePublishResponse:
        """手动模式：只投指定平台。"""

    def _publish_to_platforms(self, task_id, platforms, base_payload) -> PublishAllResult: ...
    def _publish_single(self, task_id, platform, base_payload) -> dict: ...
```

### PublishAllResult

```python
@dataclass
class PublishAllResult:
    results: dict[str, dict]  # platform -> {status, detail, job_id?, ...}

    @property
    def all_ok(self) -> bool: ...
    @property
    def has_any_ok(self) -> bool: ...
    @property
    def errors(self) -> dict[str, str]: ...
```

### 平台发现

复用 `publish_platform_settings_store.get_publish_platform_settings(db)`，返回 `{"bilibili": True, "douyin": False, ...}`。PublishService 只投 `True` 的平台。

### 错误隔离

- 某平台失败 → 记录错误，继续投下一个
- 所有平台失败 → 任务标记 failed
- 部分成功 → 不标记 failed，错误记录到结果中
- 不做账号存在性检查——启用但没配账号的平台会自然报错

## 改动清单

### 1. 新建 `src/videoroll/apps/publish_service.py`

包含 `PublishService` 类和 `PublishAllResult` 数据类。

### 2. 修改 `subtitle_service/worker.py`

- `after_render_publish()` (~line 2313)：从 httpx POST orchestrator 改为 `PublishService.publish()`
- `auto_youtube_pipeline()` (~line 2762)：从 httpx POST orchestrator 改为 `PublishService.publish()`

### 3. 修改 `orchestrator_api/routers/publishing.py`

- `enqueue_publish_job()` 端点：改为调用 `PublishService.publish_one()`
- 新增 `POST /tasks/{id}/actions/publish_all` 端点（可选，调用 `PublishService.publish()`）

### 4. 修改 `orchestrator_api/services/publishing_service.py`

- `enqueue_publish_job()` 重构为委托给 `PublishService.publish_one()`
- `build_auto_publish_after_render()` 不再需要，自动模式直接调 PublishService

### 5. 前端（可选，不阻塞后端）

- `SettingsAutoPage.tsx`：在自动模式投稿区域展示已启用平台列表
- 新增"一键投所有平台"按钮（调用 `publish_all` API）

## 不改动的部分

- `bilibili_publisher/` — 作为独立微服务不变
- `social_publisher/` — 作为独立微服务不变
- `publish_gateway.py` — 平台路由工具函数保留
- `publish_platform_settings_store.py` — 平台设置存储不变
- 数据库模型 — 无新增表或字段

## 验证方式

1. 自动模式：启用 bilibili + douyin，触发 auto_youtube_pipeline，确认两个平台都有 PublishJob 记录
2. 手动模式：单平台投稿行为不变
3. 错误隔离：禁用某平台的账号，自动模式下其他平台仍能成功
